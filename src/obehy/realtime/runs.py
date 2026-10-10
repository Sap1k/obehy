"""Rail runs and their public journeys (docs/R2_SLICE.md section 2). Pure.

The core follows a CZPTT path (PA) as one run instance keyed `czptt:pa`; the public and history
identity is the train number on its service date. A run whose train number changes is two
journeys sharing the call where it changes, linked `continues_as`. A road trip is its own
journey and its own part: nothing here applies to it.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.model import (
    Effect,
    Feed,
    JourneyKey,
    JourneySpan,
    LinkJourneys,
    PartSpan,
    ScheduledCall,
    SnapshotJourney,
)
from obehy.realtime.times import Instant

RUN_NAMESPACE = "czptt:pa"
TRAIN_NAMESPACE = "czptt:train_number"


def run_journey(feed: Feed, run: Trip, day: date) -> JourneyKey:
    """The run instance's key: the path (PA) on its service date."""

    assert run.run_key is not None
    return JourneyKey(feed, RUN_NAMESPACE, run.run_key, day)


def journey_spans(feed: Feed, run: Trip, day: date) -> tuple[JourneySpan, ...]:
    """One public journey per train number, over consecutive parts that carry it."""

    if not run.parts:
        return ()
    spans: list[JourneySpan] = []
    for part in run.parts:
        number = part.train_number
        if spans and number is not None and spans[-1].journey.key == number:
            spans[-1] = JourneySpan(spans[-1].journey, spans[-1].first, part.last)
            continue
        journey = (
            JourneyKey(feed, TRAIN_NAMESPACE, number, day)
            if number is not None
            else run_journey(feed, run, day)
        )
        spans.append(JourneySpan(journey, part.first, part.last))
    return tuple(spans)


def part_spans(run: Trip) -> tuple[PartSpan, ...]:
    return tuple(PartSpan(part.trip_id, part.first, part.last) for part in run.parts)


def snapshots(
    journey: JourneyKey,
    spans: tuple[JourneySpan, ...],
    trip: Trip,
    index: IndexView,
    at: Instant,
) -> list[Effect]:
    """The schedule snapshot of each public journey of an instance, and their links."""

    if not spans:
        return [_snapshot(journey, trip, 0, len(trip.calls) - 1, trip.trip_id, index, at, None)]
    effects: list[Effect] = []
    for span in spans:
        # The journey's first part: the one starting at its first call (a junction is also
        # the end of the part before).
        part = next(
            (p.trip_id for p in trip.parts if p.first == span.first),
            next((p.trip_id for p in trip.parts if p.first <= span.first <= p.last), trip.trip_id),
        )
        effects.append(
            _snapshot(span.journey, trip, span.first, span.last, part, index, at, trip.run_key)
        )
    for before, after in pairwise(spans):
        effects.append(LinkJourneys(before.journey, after.journey, "continues_as"))
    return effects


def _snapshot(
    journey: JourneyKey,
    trip: Trip,
    first: int,
    last: int,
    trip_id: str,
    index: IndexView,
    at: Instant,
    run_key: str | None,
) -> SnapshotJourney:
    visits: dict[str, int] = {}
    calls: list[ScheduledCall] = []
    for ordinal, call in enumerate(trip.calls[first : last + 1], start=1):
        visits[call.location_id] = visits.get(call.location_id, 0) + 1
        calls.append(
            ScheduledCall(
                ordinal,
                call.location_id,
                visits[call.location_id],
                call.passenger_service,
                call.arrival,
                call.departure,
                index.location(call.location_id).name,
            )
        )
    return SnapshotJourney(
        journey=journey,
        at=at,
        release_id=index.release_id,
        trip_id=trip_id,
        route_name=trip.route_name,
        headsign=trip.headsign,
        calls=tuple(calls),
        run_key=run_key,
    )
