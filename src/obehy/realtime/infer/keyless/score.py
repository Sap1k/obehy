"""Time evidence for one keyless candidate (BASE_PLAN.md section 19.2, step 3).

- **Departures.** Each departure of the episode is placed on the candidate: the stop left is the
  first passenger call with that name after the previous departure's. The residual is how far
  the candidate's scheduled departure there lies outside the interval the source's delay
  implies. A name the candidate does not have is no evidence (ARRIVA-Q8: the source also names
  motorway junctions); one the candidate has only before an earlier departure contradicts it.
  A departure seen further than `departure_radius_m` from the stop left keeps its place in
  the order but gives no time (ARRIVA-Q9: the name changed long after the vehicle left).
- **Position.** The fix projected onto the candidate's path, within `max_path_distance_m` and
  (with a next stop) between the call before it and the next stop, else anywhere on the path
  (ARRIVA-Q9): the residual is the timetable's lateness there minus the source's delay. The
  smallest over the projections counts.
"""

from __future__ import annotations

from dataclasses import dataclass

from obehy.realtime.geo import distance_m
from obehy.realtime.index import Call, IndexView, Trip
from obehy.realtime.infer.keyed import Match
from obehy.realtime.infer.keyless.episode import normal
from obehy.realtime.model import (
    Delay,
    Departure,
    Interval,
    KeylessEpisode,
    Observation,
    Position,
)
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.path import project_all
from obehy.realtime.timeline.plan import PlanCache
from obehy.realtime.times import Instant, ServiceTime


@dataclass(frozen=True, slots=True)
class Placed:
    """The episode laid on a candidate: per departure its residual (None without a delay), and
    the index into `trip.calls` of the current next stop (None if the episode has none)."""

    residuals: tuple[float | None, ...]
    next_stop: int | None


def _stops(trip: Trip, index: IndexView) -> list[tuple[int, str]]:
    return [
        (i, normal(index.location(call.location_id).name))
        for i, call in enumerate(trip.calls)
        if call.passenger_service
    ]


def _outside(value: Instant, interval: Interval) -> float:
    if value < interval.lo:
        return (interval.lo - value).total_seconds()
    if value > interval.hi:
        return (value - interval.hi).total_seconds()
    return 0.0


def place(match: Match, episode: KeylessEpisode, index: IndexView, policy: Policy) -> Placed | None:
    """The episode on the candidate, or None if it contradicts the candidate's calls."""

    stops = _stops(match.trip, index)
    names = [name for _, name in stops]
    day = match.journey.service_date
    residuals: list[float | None] = []
    k = 0
    for departure in episode.departures:
        name = normal(departure.stop)
        found = next((n for n in range(k, len(stops)) if names[n] == name), None)
        if found is None:
            if name in names[:k]:
                return None  # a stop the candidate has already passed: wrong order
            continue  # not a stop of the candidate (ARRIVA-Q8): no evidence either way
        call = match.trip.calls[stops[found][0]]
        seconds = call.departure if call.departure is not None else call.arrival
        if departure.scheduled is None or seconds is None or _away(departure, call, index, policy):
            residuals.append(None)
        else:
            scheduled = ServiceTime(day, seconds).instant()
            residuals.append(_outside(scheduled, departure.scheduled))
        k = found + 1
    current = None
    if episode.next_stop is not None:
        name = normal(episode.next_stop)
        found = next((n for n in range(k, len(stops)) if names[n] == name), None)
        current = None if found is None else stops[found][0]
    return Placed(tuple(residuals), current)


def _away(departure: Departure, call: Call, index: IndexView, policy: Policy) -> bool:
    """Whether the vehicle was far from the stop when its name changed (ARRIVA-Q9)."""

    where = departure.position
    location = index.location(call.location_id)
    if where is None or location.lon is None or location.lat is None:
        return False
    gap = distance_m((where.lon, where.lat), (location.lon, location.lat))
    return gap > policy.keyless.departure_radius_m


def position_residual(
    match: Match,
    placed: Placed,
    observation: Observation,
    index: IndexView,
    policy: Policy,
    plans: PlanCache,
) -> float | None:
    """Seconds between the candidate's timetable at the fix and the source's delay, or None
    when the fix is untimed, absent or off the candidate's path."""

    position = observation.first(Position)
    if position is None or observation.observed_at is None:
        return None
    plan = plans.plan(match.trip, match.journey.service_date, index, policy)
    distances = plan.path.call_distances_m
    lo, hi = float("-inf"), float("inf")
    if placed.next_stop is not None:
        margin = policy.lifecycle.arrival_radius_m
        lo = distances[max(0, placed.next_stop - 1)] - margin
        hi = distances[placed.next_stop] + margin
    delay = observation.first(Delay)
    expected = 0.0 if delay is None else float(delay.seconds)
    at = observation.observed_at
    near = [
        p
        for p in project_all(plan.path, position.lon, position.lat)
        if p.lateral_m <= policy.keyless.max_path_distance_m
    ]
    ranged = [p for p in near if lo <= p.along_m <= hi] or near
    residuals = [abs(plan.timetable.lateness_s(at, p.along_m) - expected) for p in ranged]
    return min(residuals) if residuals else None
