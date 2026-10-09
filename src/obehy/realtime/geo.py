"""Small, deterministic geometry on WGS84 coordinates (lon, lat in degrees, metres out)."""

from __future__ import annotations

import math
from itertools import pairwise

EARTH_RADIUS_M = 6_371_008.8


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lon, lat) points."""

    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def cumulative_m(points: tuple[tuple[float, float], ...]) -> tuple[float, ...]:
    """Distance travelled at each vertex of a polyline."""

    total = 0.0
    out = [0.0]
    for previous, current in pairwise(points):
        total += distance_m(previous, current)
        out.append(total)
    return tuple(out)
