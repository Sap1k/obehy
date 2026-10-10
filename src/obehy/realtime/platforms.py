"""Platform assignments from station boards (docs/R2_SLICE.md section 6). Pure.

A board row binds by train number and the date of its scheduled time like any keyed
observation, but carries no vehicle: it never creates or moves one. It attaches to the run's
call at the station (`sr70` key) whose scheduled time of that kind matches; a run without an
instance gets one, so a train at its origin has its platform before it moves. The value is the
call's shown platform; only a track maps to a static boarding point (`sr70:track`), and only
that reaches GTFS-RT. A newer assignment replaces an older; `-` never reaches here (SZT-Q4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.infer.keyed import Match
from obehy.realtime.model import (
    Effect,
    FeedState,
    Instance,
    Observation,
    ObservationResult,
    Platform,
    PlatformAssignment,
    Reason,
    RecordPlatform,
    Unresolved,
)
from obehy.realtime.times import ServiceTime

Opener = Callable[[Match], list[Effect]]
Binder = Callable[[Observation], Match | Reason]


def _call(trip: Trip, day: date, fact: PlatformAssignment, index: IndexView) -> int | None:
    for i, call in enumerate(trip.calls):
        if not call.passenger_service:
            continue
        if index.location_key(call.location_id, "sr70") != fact.station:
            continue
        seconds = call.arrival if fact.kind == "arrival" else call.departure
        if seconds is None:
            continue
        if ServiceTime(day, seconds).instant() == fact.scheduled:
            return i
    return None


def _boarding_point(fact: PlatformAssignment, index: IndexView) -> str | None:
    if fact.label != "track":
        return None  # a platform number is a label only: no stop to point at
    found = index.keyed_locations("sr70:track", f"{fact.station}:{fact.value}")
    return found[0] if len(found) == 1 else None


def apply_platform(
    state: FeedState,
    observation: Observation,
    index: IndexView,
    bind: Binder,
    open_instance: Opener,
) -> list[Effect]:
    fact = observation.first(PlatformAssignment)
    assert fact is not None
    result = bind(observation)
    if not isinstance(result, Match):
        return [ObservationResult(observation, None, result)]
    effects: list[Effect] = list(open_instance(result))
    journey = result.journey
    instance: Instance = state.instances[journey]
    i = _call(result.trip, journey.service_date, fact, index)
    if i is None:
        name = f"{fact.station} {fact.kind} {fact.scheduled.isoformat()}"
        effects.append(Unresolved(observation, "platform", name))
        effects.append(ObservationResult(observation, journey, None, instance.public))
        return effects
    platform = Platform(
        fact.value,
        fact.label,
        _boarding_point(fact, index),
        observation.received_at,
        observation.source,
    )
    current = instance.calls[i].platform
    changed = current is None or (current.value, current.label) != (platform.value, platform.label)
    calls = list(instance.calls)
    calls[i] = replace(calls[i], platform=platform if changed else current)
    freshness = replace(instance.freshness, heard_at=observation.received_at)
    state.instances[journey] = replace(instance, calls=tuple(calls), freshness=freshness)
    state.dirty.add(journey)
    public, visit = instance.public_call(i, fact.kind)
    effects.append(RecordPlatform(public, calls[i].location_id, visit, fact.kind, platform))
    effects.append(ObservationResult(observation, journey, None, instance.public))
    return effects
