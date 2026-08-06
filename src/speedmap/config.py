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

# The first and last stop of a trip get a wider exclusion: buses lay over at
# the end of a run, often parked past the stop itself rather than at it.
TERMINAL_RADIUS_M = _float("TERMINAL_RADIUS_M", 120.0)

# Bucket edge for the stop lookup index. The neighbourhood swept around a point
# widens automatically when the radius exceeds one bucket.
STOP_BUCKET_M = _float("STOP_BUCKET_M", 100.0)

# --- Depot discovery (see depots.py) -------------------------------------
# Depots and off-street layover yards are not in GTFS, so they are mined from
# the aggregates: knots of cells that are motionless, busy, and still
# motionless across many separate hours of the day, far from any stop. The
# hour count is what separates a depot from a junction that jams at rush hour.
DEPOT_MAX_KMH = _float("DEPOT_MAX_KMH", 4.0)
DEPOT_MIN_SAMPLES = _int("DEPOT_MIN_SAMPLES", 300)
DEPOT_MIN_HOURS = _int("DEPOT_MIN_HOURS", 10)
DEPOT_MIN_DIST_TO_STOP_M = _float("DEPOT_MIN_DIST_TO_STOP_M", 120.0)
# Grown onto the observed extent of a site, since parked buses are only ever
# sampled where they happen to stand.
DEPOT_PAD_M = _float("DEPOT_PAD_M", 40.0)

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
# Speed histograms are kept alongside the sums so the map can show a median (or
# any percentile) without another pass over R2. One bin per km/h; the reported
# median is the centre of the bin the middle sample falls in.
HIST_BIN_KMH = _float("HIST_BIN_KMH", 1.0)

AGG_DIR = DATA_DIR / "agg"
HIST_DIR = DATA_DIR / "hist"
STATIC_CACHE_DIR = DATA_DIR / "static_cache"
DEPOT_FILE = DATA_DIR / "depots.json"
WEB_DATA_DIR = ROOT / "web" / "data"
