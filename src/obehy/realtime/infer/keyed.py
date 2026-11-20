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

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

from obehy.realtime.index import IndexView, Trip, parent_ref
from obehy.realtime.model import Binding, JourneyKey, Reason, TripKey
from obehy.realtime.policy import Policy, TimePolicy
from obehy.realtime.runs import run_journey
from obehy.realtime.times import Instant, ServiceTime, candidate_service_dates


@dataclass(frozen=True, slots=True)
class Match:
    journey: JourneyKey
    trip: Trip
    start: Instant
    end: Instant


def span(trip: Trip, journey: JourneyKey) -> tuple[Instant, Instant]:
    return _span(journey.service_date, trip.start, trip.end)


# Both run for every fix of a bound vehicle (the binding is re-checked each time); bounded.
@lru_cache(maxsize=1 << 15)
def _span(day: date, start: int, end: int) -> tuple[Instant, Instant]:
    return ServiceTime(day, start).instant(), ServiceTime(day, end).instant()


@lru_cache(maxsize=1 << 15)
def _admissible(
    start: Instant, end: Instant, mode: str, time: TimePolicy
) -> tuple[datetime, datetime]:
    return (
        start - timedelta(seconds=time.pre_trip_s(mode)),
        end + timedelta(seconds=time.max_delay_s(mode)),
    )


def in_window(match: Match, at: Instant, policy: Policy) -> bool:
    """Whether `at` lies in the journey's admissible window (section 19.3, rule 2)."""

    lo, hi = _admissible(match.start, match.end, match.trip.mode, policy.time)
    return lo <= at <= hi


def admissible(binding: Binding, at: Instant, index: IndexView, policy: Policy) -> bool:
    """Whether `at` is still in the window of the journey `binding` holds (section 19.5): what
    keeps a binding, keyed or inferred, from one observation to the next."""

    trip = index.trip(binding.trip_id)
    return in_window(Match(binding.journey, trip, *span(trip, binding.journey)), at, policy)


def running(
    index: IndexView, namespace: str, key: str, days: Iterable[date]
) -> Iterator[tuple[date, Trip]]:
    """The feed trips a key names that run, on each of `days` in turn: the key valid on the
    day and the trip's service running it. A rail part is yielded as itself (`IndexView.run`
    joins it into its run)."""

    entries = index.keys(namespace, key)
    for day in days:
        for entry in entries:
            if not entry.valid_on(day):
                continue
            trip = index.trip(entry.public_id)
            if index.runs_on(trip.service_id, day):
                yield day, trip


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
    found: list[Match] = []
    seen: set[JourneyKey] = set()
    days = candidate_service_dates(at) if day is None else (day,)
    for candidate, trip in running(index, key.namespace, key.key, days):
        if trip.run_key is not None:
            trip = index.run(trip, candidate)
            if not trip.parts:
                return Reason.AMBIGUOUS  # two versions of a part run that day
            journey = run_journey(index.feed, trip, candidate)
            if journey in seen:
                continue  # another part of the same run
            seen.add(journey)
        else:
            journey = JourneyKey(index.feed, key.namespace, key.key, candidate)
        found.append(Match(journey, trip, *span(trip, journey)))
    if not found:
        return Reason.NOT_ACTIVE
    window = [match for match in found if in_window(match, at, policy)]
    if not window:
        return Reason.NOT_IN_SERVICE
    window.sort(key=lambda match: (_distance(match, at), match.journey.service_date))
    if len(window) > 1 and _distance(window[0], at) == _distance(window[1], at):
        return Reason.AMBIGUOUS
    return window[0]
