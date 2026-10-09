"""A trip's path in metres: shape vertices, distances and where each call lies on it.

Without a shape the path runs straight through the call locations. Calls without coordinates
get a distance interpolated by schedule time between their neighbours. Projection uses a local
equirectangular plane, which is exact enough at the scale of a tolerance of tens of metres.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

import shapely
from shapely.geometry import LineString

from obehy.realtime.geo import EARTH_RADIUS_M, cumulative_m
from obehy.realtime.index import IndexView, Trip
from obehy.realtime.policy import Policy
from obehy.realtime.times import Instant, ServiceTime


@dataclass(frozen=True, slots=True)
class Path:
    points: tuple[tuple[float, float], ...]  # (lon, lat)
    distances_m: tuple[float, ...]
    call_distances_m: tuple[float, ...]
    # Per segment: start (lon, lat) and its (dlon, dlat), precomputed for projection.
    segments: tuple[tuple[float, float, float, float], ...] = ()
    # A real shape (trusted closely) or straight chords between stops (trusted less).
    shaped: bool = False

    @property
    def length_m(self) -> float:
        return self.distances_m[-1] if self.distances_m else 0.0


@dataclass(frozen=True, slots=True)
class Projection:
    along_m: float
    lateral_m: float
    segment_m: float


@dataclass(frozen=True, slots=True)
class Candidate:
    """Where a fix may be on the path: its projection onto one segment."""

    along_m: float
    lateral_m: float
    sigma_m: float  # combined uncertainty of the fix and of the geometry of this segment
    heading_deg: float  # direction of travel along the segment, clockwise from north


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
    *,
    shaped: bool = False,
) -> Path:
    segments = tuple((ax, ay, bx - ax, by - ay) for (ax, ay), (bx, by) in pairwise(points))
    return Path(points, distances, call_distances, segments, shaped)


def candidates(
    path: Path,
    lon: float,
    lat: float,
    sigma: Callable[[Path, float], float],
    reach_sigmas: float,
    window: tuple[float, float] | None = None,
) -> list[Candidate]:
    """One candidate per segment within `reach_sigmas` of its own uncertainty (`sigma(path,
    segment length)`), optionally only segments overlapping the along-path `window`."""

    kx, ky = _plane(lat)
    distances = path.distances_m
    segments = path.segments
    first, last = 0, len(segments)
    if window is not None:
        first = max(0, bisect_left(distances, window[0]) - 1)
        last = min(last, bisect_right(distances, window[1]))
    out: list[Candidate] = []
    for i in range(first, last):
        ax, ay, dx, dy = segments[i]
        ux, uy = dx * kx, dy * ky
        px, py = (lon - ax) * kx, (lat - ay) * ky
        length2 = ux * ux + uy * uy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, (px * ux + py * uy) / length2))
        lateral = math.hypot(px - t * ux, py - t * uy)
        start, end = distances[i], distances[i + 1]
        segment = end - start
        s = sigma(path, segment)
        if lateral <= reach_sigmas * s:
            heading = math.degrees(math.atan2(ux, uy)) % 360.0
            out.append(Candidate(start + t * segment, lateral, s, heading))
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
    shape = index.shape(trip.shape_id) if trip.shape_id is not None else None
    shaped = shape is not None and len(shape.points) >= 2
    if shape is not None and shaped:
        points, distances = simplify(shape.points, shape.distances_m)
    else:
        points = tuple(point for point in located if point is not None)
        distances = cumulative_m(points)
    if len(points) < 2:
        return make_path(points, distances, tuple(0.0 for _ in trip.calls))
    known: list[float | None] = []
    after = 0.0
    for point in located:
        value: float | None = None
        if point is not None:
            value = _locate(points, distances, point[0], point[1], after)
        if value is not None:
            after = value
        known.append(value)
    times = [call.time for call in trip.calls]
    return make_path(points, distances, _fill(known, times, distances[-1]), shaped=shaped)


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


# Each call's (arrival, departure) trigger distance along the path; None where it has no event.
Triggers = tuple[tuple[float | None, float | None], ...]


def triggers(path: Path, policy: Policy) -> Triggers:
    """Each call's (arrival, departure) trigger distance; None where the call has no such event.

    Arrival triggers `arrival_radius_m` before the stop, departure `departure_margin_m` after it,
    but never past the midpoint to the neighbouring call: for stops closer together than the
    two margins, a departure must not trigger after the next arrival, so event times always
    follow call order.
    """

    radius = policy.lifecycle.arrival_radius_m
    margin = policy.lifecycle.departure_margin_m
    distances = path.call_distances_m
    last = len(distances) - 1
    out: list[tuple[float | None, float | None]] = []
    for i, distance in enumerate(distances):
        arrival = departure = None
        if i > 0:
            arrival = max(distance - radius, (distances[i - 1] + distance) / 2)
        if i < last:
            departure = min(distance + margin, (distance + distances[i + 1]) / 2)
        out.append((arrival, departure))
    return tuple(out)


# When the timetable has the vehicle at a place: (earliest, latest), None for unbounded.
Window = tuple[Instant | None, Instant | None]
Timetable = Callable[[float], Window | None]


def timetable_along(path: Path, scheduled: Scheduled, trigger_points: Triggers) -> Timetable:
    """When the timetable has the vehicle at a distance along the path.

    At a call (between its arrival and departure triggers) it is the dwell, [arrival,
    departure]; the first call has no earliest time, so waiting at the origin is never early.
    Between calls it is interpolated by distance from one
    call's departure trigger to the next call's arrival trigger. None where the timetable has no
    time.
    """

    starts: list[float] = []
    windows: list[tuple[float, float, Instant | None, Instant | None]] = []
    for i, ((arrival, departure), (arr_m, dep_m)) in enumerate(
        zip(scheduled, trigger_points, strict=True)
    ):
        at_m = path.call_distances_m[i]
        lo = None if i == 0 else (arrival if arrival is not None else departure)
        hi = departure if departure is not None else arrival
        start = -math.inf if arr_m is None else arr_m
        windows.append((start, math.inf if dep_m is None else dep_m, lo, hi))
        starts.append(start if i > 0 else at_m)

    def at(along: float) -> Window | None:
        if not windows:
            return None
        i = max(0, bisect_right(starts, along) - 1)
        start, end, lo, hi = windows[i]
        if along <= end or i == len(windows) - 1:
            return (lo, hi) if along >= start else (None, hi)
        next_start, _, next_lo, _ = windows[i + 1]
        if hi is None or next_lo is None:
            return None
        span = next_start - end
        share = 0.0 if span <= 0 else (along - end) / span
        planned = Instant(hi + (next_lo - hi) * share)
        return planned, planned

    return at


@dataclass(frozen=True, slots=True)
class Plan:
    """A journey's path with its timetable laid on it: built once per trip and service date."""

    path: Path
    scheduled: Scheduled
    triggers: Triggers
    timetable: Timetable
    # Every trigger in path order: (distance, call index, 0 arrival / 1 departure).
    events: tuple[tuple[float, int, int], ...] = ()
    event_distances: tuple[float, ...] = ()


@dataclass(slots=True)
class PathCache:
    """Paths by trip and plans by journey for one release; deterministic, so caching never
    changes output."""

    release_id: str = ""
    paths: dict[str, Path] = field(default_factory=dict[str, Path])
    plans: dict[tuple[str, date], Plan] = field(default_factory=dict[tuple[str, date], Plan])

    def scheduled(self, trip: Trip, day: date) -> Scheduled:
        """Each call's (arrival, departure) instants on service date `day`."""

        return tuple(
            (
                None if c.arrival is None else ServiceTime(day, c.arrival).instant(),
                None if c.departure is None else ServiceTime(day, c.departure).instant(),
            )
            for c in trip.calls
        )

    def get(self, trip: Trip, index: IndexView) -> Path:
        if index.release_id != self.release_id:
            self.release_id = index.release_id
            self.paths.clear()
            self.plans.clear()
        path = self.paths.get(trip.trip_id)
        if path is None:
            path = build_path(trip, index)
            self.paths[trip.trip_id] = path
        return path

    def plan(self, trip: Trip, day: date, index: IndexView, policy: Policy) -> Plan:
        path = self.get(trip, index)
        key = (trip.trip_id, day)
        value = self.plans.get(key)
        if value is None:
            scheduled = self.scheduled(trip, day)
            points = triggers(path, policy)
            events = sorted(
                (trigger, i, rank)
                for i, pair in enumerate(points)
                for rank, trigger in enumerate(pair)
                if trigger is not None
            )
            value = Plan(
                path,
                scheduled,
                points,
                timetable_along(path, scheduled, points),
                tuple(events),
                tuple(e[0] for e in events),
            )
            if len(self.plans) > 50_000:
                self.plans.clear()
            self.plans[key] = value
        return value
