"""
Pure-Python UTM zone 35N (EPSG:32635) forward projection.

Implements the Snyder (1987) Transverse Mercator series, accurate to < 1 mm
anywhere in zone 35N.  No external dependencies — runs in Pyodide / CF Workers.

Only exports one function: project_xy(lon, lat) → (easting_m, northing_m)
"""

from __future__ import annotations
import math

# WGS-84 ellipsoid
_a = 6_378_137.0
_f = 1.0 / 298.257_223_563
_e2 = 2 * _f - _f * _f          # first eccentricity squared  ≈ 0.006694
_ep2 = _e2 / (1.0 - _e2)        # second eccentricity squared ≈ 0.006740
_k0 = 0.9996                     # UTM scale factor
_E0 = 500_000.0                  # false easting (m)
_N0 = 0.0                        # false northing, northern hemisphere
_lon0 = math.radians(27.0)       # central meridian of zone 35N

# Meridional arc coefficients (Helmert series, Snyder eq. 3-21)
_c0 = 1 - _e2 / 4 - 3 * _e2**2 / 64 - 5 * _e2**3 / 256
_c2 = 3 * _e2 / 8 + 3 * _e2**2 / 32 + 45 * _e2**3 / 1024
_c4 = 15 * _e2**2 / 256 + 45 * _e2**3 / 1024
_c6 = 35 * _e2**3 / 3072


def project_xy(lon: float, lat: float) -> tuple[float, float]:
    """WGS-84 (lon, lat) degrees → UTM zone 35N (easting, northing) metres."""
    phi = math.radians(lat)
    dlam = math.radians(lon) - _lon0

    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    tan_phi = sin_phi / cos_phi

    # Radius of curvature in the prime vertical (Snyder eq. 4-15)
    N = _a / math.sqrt(1.0 - _e2 * sin_phi * sin_phi)

    T = tan_phi * tan_phi
    C = _ep2 * cos_phi * cos_phi
    A = cos_phi * dlam

    # Meridional arc from equator to phi (Snyder eq. 3-21)
    M = _a * (
        _c0 * phi
        - _c2 * math.sin(2 * phi)
        + _c4 * math.sin(4 * phi)
        - _c6 * math.sin(6 * phi)
    )

    # Snyder eq. 8-9 (easting)
    x = (
        _k0
        * N
        * (
            A
            + (1 - T + C) * A**3 / 6
            + (5 - 18 * T + T**2 + 72 * C - 58 * _ep2) * A**5 / 120
        )
        + _E0
    )

    # Snyder eq. 8-10 (northing)
    y = (
        _k0
        * (
            M
            + N
            * tan_phi
            * (
                A**2 / 2
                + (5 - T + 9 * C + 4 * C**2) * A**4 / 24
                + (61 - 58 * T + T**2 + 600 * C - 330 * _ep2) * A**6 / 720
            )
        )
        + _N0
    )

    return x, y
