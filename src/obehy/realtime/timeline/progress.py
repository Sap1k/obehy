"""Progress along the path from GPS fixes (BASE_PLAN.md sections 20.4, 20.7, 21.2).

Progress never goes backwards. A fix far behind current progress (beyond the backtrack
tolerance) is ignored, never used to rewind. A fix further from the path than
`max(base(mode), k * segment length)` holds progress; after `off_route_hold_s` the instance is
flagged off-route, keeping its keyed binding. Returning within tolerance clears the flag.
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


def call_index_at(path: Path, along_m: float, policy: Policy) -> int:
    """The last call the vehicle has left: its departure trigger (or, for the last call, its
    arrival trigger) is at or behind `along_m`; -1 before the first departure."""

    last = len(path.call_distances_m) - 1
    index = -1
    for i, distance in enumerate(path.call_distances_m):
        trigger = (
            distance - policy.lifecycle.arrival_radius_m
            if i == last
            else distance + policy.lifecycle.departure_margin_m
        )
        if trigger <= along_m:
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
    base, k = lifecycle.off_route_base_m(mode), lifecycle.off_route_k
    chosen = None
    if previous is not None:
        # Only where the vehicle can be: from just behind progress to as far as it could have
        # driven since the last fix. After a long gap nothing fits and the whole path is used.
        reach = lifecycle.max_speed_mps(mode) * max(0.0, (at - previous.at).total_seconds())
        window = (
            previous.distance_m - lifecycle.backtrack_tolerance_m,
            previous.distance_m + reach + base,
        )
        near = project_near(path, position.lon, position.lat, base, k, window)
        chosen = _choose(near, previous.distance_m, policy)
    if chosen is None:
        near = project_near(path, position.lon, position.lat, base, k)
        chosen = _choose(near, None if previous is None else previous.distance_m, policy)
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

    radius = policy.lifecycle.arrival_radius_m
    margin = policy.lifecycle.departure_margin_m
    last = len(path.call_distances_m) - 1
    when = Interval(t0, t1)
    out: list[tuple[int, EventKind, Interval]] = []
    for i, distance in enumerate(path.call_distances_m):
        if i > 0 and start < distance - radius <= end:
            out.append((i, "arrival", when))
        if i < last and start < distance + margin <= end:
            out.append((i, "departure", when))
    return tuple(out)
