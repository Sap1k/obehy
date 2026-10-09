"""The timeline engine: progress, events, delay and estimates of one journey instance.

R1 has one source per journey, so arbitration is a pass-through: the source delay (a weak
constraint with an unknown reference, DUK-Q6) drives predictions, GPS progress (map matching
over the trip's path, `progress.py`) drives actual events, committed once unambiguous.
Multi-source fusion (BASE_PLAN.md section 20.6) arrives with SŽ in R2.
"""

from __future__ import annotations

from dataclasses import replace

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.model import (
    Delay,
    Effect,
    Instance,
    Observation,
    Position,
    WriteEvent,
)
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.path import PathCache
from obehy.realtime.timeline.progress import update


def advance(
    instance: Instance,
    trip: Trip,
    observation: Observation,
    index: IndexView,
    policy: Policy,
    paths: PathCache,
) -> tuple[Instance, list[Effect]]:
    """Apply one bound observation to its instance."""

    effects: list[Effect] = []
    delay = observation.first(Delay)
    delay_s = instance.delay_s
    if delay is not None and delay.seconds > policy.delay_discard_below_s:
        delay_s = delay.seconds
    instance = replace(instance, updated_at=observation.at, delay_s=delay_s)

    position = observation.first(Position)
    seen = instance.track.seen_at if instance.track is not None else None
    # A fix no newer than the last one used (repeated or out of order, or a GPS time frozen
    # while payloads keep coming) is no new information: it never moves progress, so event
    # intervals always run forward in time, and it does not keep the journey fresh.
    repeated = position is not None and seen is not None and observation.at <= seen
    if not repeated:
        instance = replace(instance, heard_at=observation.received_at)
    if instance.lifecycle == "running" and position is not None and not repeated:
        movement = update(
            paths.plan(trip, instance.journey.service_date, index, policy),
            instance.track,
            position,
            observation.at,
            trip.mode,
            policy,
        )
        calls = list(instance.calls)
        for call_index, kind, fixes, when, recorded in movement.crossed:
            state = calls[call_index]
            if kind == "arrival":
                state = replace(state, passed_arrival=when)
                if recorded:
                    state = replace(state, arrival=fixes)
            else:
                state = replace(state, passed_departure=when)
                if recorded:
                    state = replace(state, departure=fixes)
            calls[call_index] = state
            if not recorded:
                continue  # passed inside a reception gap: realtime only, no history event
            effects.append(
                WriteEvent(
                    instance.journey,
                    state.location_id,
                    state.visit_n,
                    kind,
                    fixes,
                    "progress",
                    observation.source,
                )
            )
        finished = any(
            kind == "arrival" and i == len(calls) - 1 for i, kind, *_ in movement.crossed
        )
        instance = replace(
            instance,
            calls=tuple(calls),
            progress=movement.progress,
            track=movement.track,
            off_route_since=movement.off_route_since,
            off_route=movement.off_route,
            lifecycle="finished" if finished else instance.lifecycle,
        )
    return instance, effects
