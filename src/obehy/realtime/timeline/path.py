"""A trip's path in metres: shape vertices, distances and where each call lies on it.

Without a shape the path runs straight through the call locations. Calls without coordinates
get a distance interpolated by schedule time between their neighbours. Projection uses a local
equirectangular plane, which is exact enough at the scale of a tolerance of tens of metres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from obehy.realtime.geo import EARTH_RADIUS_M, cumulative_m
from obehy.realtime.index import IndexView, Trip


@dataclass(frozen=True, slots=True)
class Path:
    points: tuple[tuple[float, float], ...]  # (lon, lat)
    distances_m: tuple[float, ...]
    call_distances_m: tuple[float, ...]

    @property
    def length_m(self) -> float:
        return self.distances_m[-1] if self.distances_m else 0.0


@dataclass(frozen=True, slots=True)
class Projection:
    along_m: float
    lateral_m: float
    segment_m: float


def _plane(lat0: float) -> tuple[float, float]:
    rad = math.pi / 180 * EARTH_RADIUS_M
    return rad * math.cos(math.radians(lat0)), rad


def project_all(path: Path, lon: float, lat: float) -> list[Projection]:
    """The point's projection onto every segment of the path."""

    kx, ky = _plane(lat)
    out: list[Projection] = []
    for i in range(len(path.points) - 1):
        (ax, ay), (bx, by) = path.points[i], path.points[i + 1]
        ux, uy = (bx - ax) * kx, (by - ay) * ky
        px, py = (lon - ax) * kx, (lat - ay) * ky
        length2 = ux * ux + uy * uy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, (px * ux + py * uy) / length2))
        dx, dy = px - t * ux, py - t * uy
        start, end = path.distances_m[i], path.distances_m[i + 1]
        out.append(Projection(start + t * (end - start), math.hypot(dx, dy), end - start))
    return out


def _locate(
    path_points: tuple[tuple[float, float], ...],
    distances: tuple[float, ...],
    lon: float,
    lat: float,
    after_m: float,
) -> float:
    """Distance of a stop on the path: its nearest projection at or after `after_m`."""

    probe = Path(path_points, distances, ())
    candidates = [p for p in project_all(probe, lon, lat) if p.along_m >= after_m - 1e-6]
    if not candidates:
        return after_m
    return min(candidates, key=lambda p: (p.lateral_m, p.along_m)).along_m


def build_path(trip: Trip, index: IndexView) -> Path:
    located: list[tuple[float, float] | None] = []
    for call in trip.calls:
        location = index.location(call.location_id)
        lon, lat = location.lon, location.lat
        located.append((lon, lat) if lon is not None and lat is not None else None)
    if trip.shape_id is not None:
        shape = index.shape(trip.shape_id)
        points, distances = shape.points, shape.distances_m
    else:
        points = tuple(point for point in located if point is not None)
        distances = cumulative_m(points)
    if len(points) < 2:
        return Path(points, distances, tuple(0.0 for _ in trip.calls))
    known: list[float | None] = []
    after = 0.0
    for call, point in zip(trip.calls, located, strict=True):
        if call.distance_m is not None and trip.shape_id is not None:
            value: float | None = max(call.distance_m, after)
        elif point is not None:
            value = _locate(points, distances, point[0], point[1], after)
        else:
            value = None
        if value is not None:
            after = value
        known.append(value)
    times = [call.time for call in trip.calls]
    return Path(points, distances, _fill(known, times, distances[-1]))


def _fill(known: list[float | None], times: list[int], length: float) -> tuple[float, ...]:
    """Interpolate missing call distances by schedule time between known neighbours."""

    out: list[float] = []
    for i, value in enumerate(known):
        if value is not None:
            out.append(value)
            continue
        before = next(
            ((times[j], known[j]) for j in range(i - 1, -1, -1) if known[j] is not None), None
        )
        after = next(
            ((times[j], known[j]) for j in range(i + 1, len(known)) if known[j] is not None), None
        )
        lo_t, lo_d = before if before is not None else (times[0], 0.0)
        hi_t, hi_d = after if after is not None else (times[-1], length)
        assert lo_d is not None and hi_d is not None
        share = 0.0 if hi_t == lo_t else (times[i] - lo_t) / (hi_t - lo_t)
        out.append(lo_d + share * (hi_d - lo_d))
    return tuple(out)


@dataclass(slots=True)
class PathCache:
    """Paths by trip of one release; deterministic, so caching never changes output."""

    release_id: str = ""
    paths: dict[str, Path] = field(default_factory=dict[str, Path])

    def get(self, trip: Trip, index: IndexView) -> Path:
        if index.release_id != self.release_id:
            self.release_id = index.release_id
            self.paths.clear()
        path = self.paths.get(trip.trip_id)
        if path is None:
            path = build_path(trip, index)
            self.paths[trip.trip_id] = path
        return path
