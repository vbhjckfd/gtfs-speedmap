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

# The popup's hourly sparkline splits a cell 19 ways, so MIN_SAMPLES applied per
# hour leaves half the cells with three bars or fewer — which reads as "no buses
# ran at 14:00" rather than "not measured". A lower floor lifts the median cell
# from 6 populated hours to 9; cells that still cannot fill PROFILE_MIN_HOURS
# get no sparkline at all, because a two-bar chart misleads more than it tells.
PROFILE_MIN_SAMPLES = _int("PROFILE_MIN_SAMPLES", 3)
PROFILE_MIN_HOURS = _int("PROFILE_MIN_HOURS", 6)

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

# --- Relative speed ("% of free-flow") -----------------------------------
# A cell's free-flow reference is its own p85 over every hour, month and day
# type, so the ratio answers "how congested is this bit of road right now"
# rather than "is this bit of road fast" — the two are near-uncorrelated
# (measured r = 0.07). The reference must be global: derive it per selection
# and the colours shift meaninglessly as the slider moves.
FREE_FLOW_Q = _float("FREE_FLOW_Q", 0.85)
# Below this, the ratio is noise over noise: a cell whose free-flow is 5 km/h
# has no free flow to speak of. Measured reference speeds run p25 18.5,
# p50 28.5, p75 41.5 km/h, so this discards very little.
REL_MIN_FF_KMH = _float("REL_MIN_FF_KMH", 8.0)
REL_SCALE_LOW = _float("REL_SCALE_LOW", 0.35)
REL_SCALE_HIGH = _float("REL_SCALE_HIGH", 1.0)

# Spread (p85 − p15) is unreliability, so its ramp runs the other way: a wide
# spread is the bad end.
SPREAD_SCALE_LOW_KMH = _float("SPREAD_SCALE_LOW_KMH", 5.0)
SPREAD_SCALE_HIGH_KMH = _float("SPREAD_SCALE_HIGH_KMH", 30.0)

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
