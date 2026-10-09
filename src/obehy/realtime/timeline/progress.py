"""Progress along the path from GPS fixes (BASE_PLAN.md sections 20.4, 20.7, 21.2).

Progress never goes backwards. A fix far behind current progress (beyond the backtrack
tolerance) is ignored, never used to rewind. A fix further from the path than
`max(base(mode), min(k * segment length, cap))` holds progress; after `off_route_hold_s` the
instance is flagged off-route, keeping its keyed binding. Returning within tolerance clears it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from obehy.realtime.model import EventKind, Interval, Position, Progress
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.path import Path, Projection, project_near
from obehy.realtime.times import Instant


@dataclass(frozen=True, slots=True)
class Movement:
    progress: Progress | None
    off_route_since: Instant | None
    off_route: bool
    crossed: tuple[tuple[int, EventKind, Interval], ...]  # (call index, kind, when)


def _choose(near: list[Projection], previous: float | None, policy: Policy) -> Projection | None:
    """The on-route projection closest ahead of current progress (loops visit a place twice)."""

    if not near:
        return None
    if previous is None:
        return min(near, key=lambda p: (p.lateral_m, p.along_m))
    floor = previous - policy.lifecycle.backtrack_tolerance_m
    ahead = [p for p in near if p.along_m >= floor]
    if not ahead:
        return min(near, key=lambda p: (abs(p.along_m - previous), p.lateral_m))
    # Closest to the path first; a loop's later visit loses the tie to the nearer one ahead.
    return min(ahead, key=lambda p: (round(p.lateral_m, 1), p.along_m))


def triggers(path: Path, policy: Policy) -> list[tuple[float | None, float | None]]:
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
    return out


def call_index_at(path: Path, along_m: float, policy: Policy) -> int:
    """The last call the vehicle has left: its departure trigger (or, for the last call, its
    arrival trigger) is at or behind `along_m`; -1 before the first departure."""

    index = -1
    for i, (arrival, departure) in enumerate(triggers(path, policy)):
        trigger = departure if departure is not None else arrival
        if trigger is not None and trigger <= along_m:
            index = i
    return index


def move(
    path: Path,
    previous: Progress | None,
    off_route_since: Instant | None,
    position: Position,
    at: Instant,
    mode: str,
    policy: Policy,
) -> Movement:
    if len(path.points) < 2:
        return Movement(previous, None, False, ())
    lifecycle = policy.lifecycle
    base, k, cap = (
        lifecycle.off_route_base_m(mode),
        lifecycle.off_route_k,
        lifecycle.off_route_max_m,
    )
    if previous is None:
        # The first fix may be anywhere on the path; ties go to the earliest visit.
        near = project_near(path, position.lon, position.lat, base, k, cap)
        chosen = _choose(near, None, policy)
    else:
        # Only where the vehicle can have got to since the last fix used: never further, so a
        # detour that returns along its way out (a závlek) is never skipped by a fix that
        # happens to lie closer to the way back. The reach grows with the time since that fix,
        # which covers data gaps; a fix with nothing within reach is off-route, not a jump.
        reach = lifecycle.max_speed_mps(mode) * max(0.0, (at - previous.at).total_seconds())
        window = (
            previous.distance_m - lifecycle.backtrack_tolerance_m,
            previous.distance_m + reach + base,
        )
        near = project_near(path, position.lon, position.lat, base, k, cap, window)
        chosen = _choose(near, previous.distance_m, policy)
    if chosen is None:
        since = off_route_since or at
        held = at - since >= timedelta(seconds=policy.lifecycle.off_route_hold_s)
        return Movement(previous, since, held, ())
    if previous is None:
        start = Progress(chosen.along_m, call_index_at(path, chosen.along_m, policy), at)
        return Movement(start, None, False, ())
    if chosen.along_m < previous.distance_m:
        # Behind current progress but within tolerance: GPS jitter, progress holds.
        return Movement(Progress(previous.distance_m, previous.call_index, at), None, False, ())
    crossed = _crossed(path, previous.distance_m, chosen.along_m, previous.at, at, policy)
    progress = Progress(chosen.along_m, call_index_at(path, chosen.along_m, policy), at)
    return Movement(progress, None, False, crossed)


def _crossed(
    path: Path, start: float, end: float, t0: Instant, t1: Instant, policy: Policy
) -> tuple[tuple[int, EventKind, Interval], ...]:
    """Events whose trigger distance lies in (start, end]; bounded by the two fixes."""

    when = Interval(t0, t1)
    out: list[tuple[int, EventKind, Interval]] = []
    for i, (arrival, departure) in enumerate(triggers(path, policy)):
        if arrival is not None and start < arrival <= end:
            out.append((i, "arrival", when))
        if departure is not None and start < departure <= end:
            out.append((i, "departure", when))
    return tuple(out)
