"""The time-of-day axis every aggregate is keyed by.

Half an hour, not a whole one. A rush hour is not flat: the 08:00–08:29 half of
it and the 08:30–08:59 half differ by more than the gap between two neighbouring
hours off-peak, and an hourly bin averages that difference away — which is
exactly the detail someone looking at a rush hour came for.

A slot is an integer index from midnight local time, so it sorts, subtracts and
groups like the hour it replaces. `SLOT_MINUTES` is the only knob; everything
here derives from it, including the viewer's labels via index.json.
"""

from __future__ import annotations

from datetime import datetime

from .config import SLOT_MINUTES

SLOTS_PER_DAY = (24 * 60) // SLOT_MINUTES


def slot_of(local: datetime) -> int:
    """Which slot a local-time moment falls in."""
    return (local.hour * 60 + local.minute) // SLOT_MINUTES


def slot_start(slot: int) -> tuple[int, int]:
    """(hour, minute) the slot opens at."""
    minutes = slot * SLOT_MINUTES
    return minutes // 60, minutes % 60


def slot_key(slot: int) -> str:
    """The slot as it appears in a payload filename: 0830, not 8.5."""
    hour, minute = slot_start(slot)
    return f"{hour:02d}{minute:02d}"


def slot_label(slot: int) -> str:
    """08:30 — the moment the slot opens."""
    hour, minute = slot_start(slot)
    return f"{hour:02d}:{minute:02d}"
