"""Candidate journeys of a keyless observation (BASE_PLAN.md section 19.2, steps 1-2).

The line's trips (`IndexView.line_keys`) on the candidate service dates, hard-filtered: the trip
runs that day, its last passenger stop is the destination (matched only against the trip's own
calls, section 19.1), and its schedule widened by `candidate_window_s` covers the observation.
A journey is keyed by the trip's own literal key, so the same trip bound by a keyed source is
the same journey.
"""

from __future__ import annotations

from datetime import timedelta

from obehy.realtime.index import CHILD_NAMESPACE, IndexView, Trip
from obehy.realtime.infer.keyed import Match, span
from obehy.realtime.infer.keyless.episode import same_name
from obehy.realtime.model import JourneyKey, LineRef, Reason
from obehy.realtime.policy import KeylessPolicy
from obehy.realtime.times import Instant, candidate_service_dates


def destination_of(trip: Trip, index: IndexView) -> str | None:
    passenger = [call for call in trip.calls if call.passenger_service]
    return index.location(passenger[-1].location_id).name if passenger else None


def candidates(
    line: LineRef, destination: str, at: Instant, index: IndexView, policy: KeylessPolicy
) -> list[Match] | Reason:
    if line.namespace not in CHILD_NAMESPACE:
        return Reason.NO_LINE
    if not index.keys(line.namespace, line.value):
        return Reason.NO_LINE
    namespace = CHILD_NAMESPACE[line.namespace]
    keys = index.line_keys(line.namespace, line.value)
    if not keys:
        return Reason.NO_TRIP
    window = timedelta(seconds=policy.candidate_window_s)
    running = False
    found: list[Match] = []
    for day in candidate_service_dates(at):
        for key in keys:
            for entry in index.keys(namespace, key):
                if not entry.valid_on(day):
                    continue
                trip = index.trip(entry.public_id)
                if trip.run_key is not None or not index.runs_on(trip.service_id, day):
                    continue
                running = True
                last = destination_of(trip, index)
                if last is None or not same_name(last, destination):
                    continue
                journey = JourneyKey(index.feed, namespace, key, day)
                start, end = span(trip, journey)
                if start - window <= at <= end + window:
                    found.append(Match(journey, trip, start, end))
    if not found:
        return Reason.NOT_IN_SERVICE if running else Reason.NOT_ACTIVE
    return sorted(found, key=lambda match: match.journey)
