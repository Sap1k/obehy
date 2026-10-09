"""A trip's path in metres: shape vertices, distances and where each call lies on it.

Without a shape the path runs straight through the call locations. Calls without coordinates
get a distance interpolated by schedule time between their neighbours. Projection uses a local
equirectangular plane, which is exact enough at the scale of a tolerance of tens of metres.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

import shapely
from shapely.geometry import LineString

from obehy.realtime.geo import EARTH_RADIUS_M, cumulative_m
from obehy.realtime.index import IndexView, Trip
from obehy.realtime.times import Instant, ServiceTime


@dataclass(frozen=True, slots=True)
class Path:
    points: tuple[tuple[float, float], ...]  # (lon, lat)
    distances_m: tuple[float, ...]
    call_distances_m: tuple[float, ...]
    # Per segment: start (lon, lat) and its (dlon, dlat), precomputed for projection.
    segments: tuple[tuple[float, float, float, float], ...] = ()

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


def make_path(
    points: tuple[tuple[float, float], ...],
    distances: tuple[float, ...],
    call_distances: tuple[float, ...],
) -> Path:
    segments = tuple((ax, ay, bx - ax, by - ay) for (ax, ay), (bx, by) in pairwise(points))
    return Path(points, distances, call_distances, segments)


def project_near(
    path: Path,
    lon: float,
    lat: float,
    base_m: float,
    k: float,
    window: tuple[float, float] | None = None,
) -> list[Projection]:
    """Projections onto the segments the point is within tolerance of,
    `max(base_m, k * segment length)`, optionally only segments overlapping the along-path
    `window` (lo, hi) in metres."""

    kx, ky = _plane(lat)
    distances = path.distances_m
    segments = path.segments
    first, last = 0, len(segments)
    if window is not None:
        first = max(0, bisect_left(distances, window[0]) - 1)
        last = min(last, bisect_right(distances, window[1]))
    out: list[Projection] = []
    for i in range(first, last):
        ax, ay, dx, dy = segments[i]
        ux, uy = dx * kx, dy * ky
        px, py = (lon - ax) * kx, (lat - ay) * ky
        length2 = ux * ux + uy * uy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, (px * ux + py * uy) / length2))
        lateral = math.hypot(px - t * ux, py - t * uy)
        start, end = distances[i], distances[i + 1]
        segment = end - start
        if lateral <= max(base_m, k * segment):
            out.append(Projection(start + t * segment, lateral, segment))
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


# About 5 m: far below any off-route tolerance, and dense shapes shrink to a few dozen vertices.
SIMPLIFY_DEGREES = 0.00005


def simplify(
    points: tuple[tuple[float, float], ...], distances: tuple[float, ...]
) -> tuple[tuple[tuple[float, float], ...], tuple[float, ...]]:
    """Drop vertices within ~5 m of the line through their neighbours. Kept vertices keep their
    original along-path distances, so call distances and progress stay on the same scale."""

    if len(points) <= 2:
        return points, distances
    kept = shapely.simplify(LineString(points), SIMPLIFY_DEGREES, preserve_topology=False)
    coordinates = [(float(x), float(y)) for x, y in kept.coords]
    chosen: list[int] = []
    cursor = 0
    for coordinate in coordinates:
        while cursor < len(points) and points[cursor] != coordinate:
            cursor += 1
        if cursor == len(points):
            return points, distances  # cannot map back: keep the full shape
        chosen.append(cursor)
        cursor += 1
    if not chosen or chosen[0] != 0 or chosen[-1] != len(points) - 1:
        return points, distances
    return tuple(points[i] for i in chosen), tuple(distances[i] for i in chosen)


def build_path(trip: Trip, index: IndexView) -> Path:
    located: list[tuple[float, float] | None] = []
    for call in trip.calls:
        location = index.location(call.location_id)
        lon, lat = location.lon, location.lat
        located.append((lon, lat) if lon is not None and lat is not None else None)
    if trip.shape_id is not None:
        shape = index.shape(trip.shape_id)
        points, distances = simplify(shape.points, shape.distances_m)
    else:
        points = tuple(point for point in located if point is not None)
        distances = cumulative_m(points)
    if len(points) < 2:
        return make_path(points, distances, tuple(0.0 for _ in trip.calls))
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
    return make_path(points, distances, _fill(known, times, distances[-1]))


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


Scheduled = tuple[tuple[Instant | None, Instant | None], ...]


@dataclass(slots=True)
class PathCache:
    """Paths by trip and scheduled instants by journey for one release; deterministic, so
    caching never changes output."""

    release_id: str = ""
    paths: dict[str, Path] = field(default_factory=dict[str, Path])
    schedules: dict[tuple[str, date], Scheduled] = field(
        default_factory=dict[tuple[str, date], Scheduled]
    )

    def scheduled(self, trip: Trip, day: date) -> Scheduled:
        """Each call's (arrival, departure) instants on service date `day`."""

        key = (trip.trip_id, day)
        value = self.schedules.get(key)
        if value is None:
            value = tuple(
                (
                    None if c.arrival is None else ServiceTime(day, c.arrival).instant(),
                    None if c.departure is None else ServiceTime(day, c.departure).instant(),
                )
                for c in trip.calls
            )
            if len(self.schedules) > 50_000:
                self.schedules.clear()
            self.schedules[key] = value
        return value

    def get(self, trip: Trip, index: IndexView) -> Path:
        if index.release_id != self.release_id:
            self.release_id = index.release_id
            self.paths.clear()
            self.schedules.clear()
        path = self.paths.get(trip.trip_id)
        if path is None:
            path = build_path(trip, index)
            self.paths[trip.trip_id] = path
        return path
