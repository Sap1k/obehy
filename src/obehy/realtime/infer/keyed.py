"""Keyed binding: a literal source key plus the observation time choose one journey.

The date rule of BASE_PLAN.md section 19.3: candidate service dates are local yesterday, today and
tomorrow; a date survives when the trip runs on it and the observation lies in
`[start - pre_trip(mode), end + max_delay(mode)]`. The closest survivor wins; a tie is ambiguous.
Keys are never reinterpreted (DUK-Q3): a key whose trip does not run is unmatched.

Rail (docs/R2_SLICE.md section 2): a matched trip part is lifted to its run on that date, and
the instance is the run (`czptt:pa`), whichever key found it, so a train number from DÚK and a
TR from SŽ bind the same instance. A source that names the operating date (`ServiceDay`, SŽ)
makes it the only candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from obehy.realtime.index import IndexView, Trip, parent_ref
from obehy.realtime.model import JourneyKey, Reason, TripKey
from obehy.realtime.policy import Policy
from obehy.realtime.runs import run_journey
from obehy.realtime.times import Instant, ServiceTime, candidate_service_dates


@dataclass(frozen=True, slots=True)
class Match:
    journey: JourneyKey
    trip: Trip
    start: Instant
    end: Instant


def span(trip: Trip, journey: JourneyKey) -> tuple[Instant, Instant]:
    return (
        ServiceTime(journey.service_date, trip.start).instant(),
        ServiceTime(journey.service_date, trip.end).instant(),
    )


def in_window(match: Match, at: Instant, policy: Policy) -> bool:
    """Whether `at` lies in the journey's admissible window (section 19.3, rule 2)."""

    mode = match.trip.mode
    lo = match.start - timedelta(seconds=policy.time.pre_trip_s(mode))
    hi = match.end + timedelta(seconds=policy.time.max_delay_s(mode))
    return lo <= at <= hi


def _distance(match: Match, at: Instant) -> timedelta:
    if at < match.start:
        return match.start - at
    if at > match.end:
        return at - match.end
    return timedelta(0)


def bind(
    key: TripKey, at: Instant, index: IndexView, policy: Policy, day: date | None = None
) -> Match | Reason:
    entries = index.keys(key.namespace, key.key)
    if not entries:
        parent = parent_ref((key.namespace, key.key))
        if parent is not None and not index.keys(*parent):
            return Reason.NO_LINE
        return Reason.NO_TRIP
    running: list[Match] = []
    seen: set[JourneyKey] = set()
    for candidate in candidate_service_dates(at) if day is None else (day,):
        for entry in entries:
            if not entry.valid_on(candidate):
                continue
            trip = index.trip(entry.public_id)
            if not index.runs_on(trip.service_id, candidate):
                continue
            if trip.run_key is not None:
                trip = index.run(trip, candidate)
                journey = run_journey(index.feed, trip, candidate)
            else:
                journey = JourneyKey(index.feed, key.namespace, key.key, candidate)
            if journey in seen:
                continue  # another part of the same run
            seen.add(journey)
            running.append(Match(journey, trip, *span(trip, journey)))
    if not running:
        return Reason.NOT_ACTIVE
    admissible = [match for match in running if in_window(match, at, policy)]
    if not admissible:
        return Reason.NOT_IN_SERVICE
    admissible.sort(key=lambda match: (_distance(match, at), match.journey.service_date))
    if len(admissible) > 1 and _distance(admissible[0], at) == _distance(admissible[1], at):
        return Reason.AMBIGUOUS
    return admissible[0]
