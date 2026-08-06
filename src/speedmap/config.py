"""Tunables for the aggregation pipeline. Every value is overridable via env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# Grid cell edge in metres (UTM 35N eastings/northings).
CELL_SIZE_M = _float("CELL_SIZE_M", 25.0)

# A position within this distance of a stop *on its own trip* is dropped, so the
# map shows running speed rather than dwell time.
STOP_RADIUS_M = _float("STOP_RADIUS_M", 40.0)

# Bucket edge for the stop lookup index. Must be >= STOP_RADIUS_M so that a
# 3x3 neighbourhood of buckets is guaranteed to contain every stop in range.
STOP_BUCKET_M = _float("STOP_BUCKET_M", 100.0)

# The feed republishes vehicles whose own timestamp is hours old; those are
# ghosts, not observations.
STALE_MAX_S = _int("STALE_MAX_S", 120)

# 25 m/s = 90 km/h. Anything above is GPS noise for a city bus.
SPEED_MAX_MPS = _float("SPEED_MAX_MPS", 25.0)

# Cells with fewer samples than this are dropped from the web output.
MIN_SAMPLES = _int("MIN_SAMPLES", 5)

# Hours and months are local-time concepts; the feed is UTC.
TZ = os.environ.get("TZ_LOCAL", "Europe/Kyiv")

# Concurrent R2 GETs.
WORKERS = _int("WORKERS", 16)

# Lviv bounding box (lat_min, lat_max, lon_min, lon_max) — a coarse sanity gate.
BBOX = (49.70, 49.95, 23.85, 24.20)

# Colour-scale domain in km/h: at or below LOW is full red, at or above HIGH is
# full green. Baked into index.json so the legend and the dots agree.
# Measured cell speeds run p25≈13, p50≈21, p90≈41 km/h over a whole day, so this
# domain puts the median near the middle of the ramp — rush hour then reads
# orange-red against a greener off-peak instead of everything looking slow.
SCALE_LOW_KMH = _float("SCALE_LOW_KMH", 5.0)
SCALE_HIGH_KMH = _float("SCALE_HIGH_KMH", 35.0)

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
AGG_DIR = DATA_DIR / "agg"
STATIC_CACHE_DIR = DATA_DIR / "static_cache"
WEB_DATA_DIR = ROOT / "web" / "data"
