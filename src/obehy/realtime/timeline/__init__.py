"""The timeline engine: progress, events, delay and estimates of one journey instance.

R1 has one source per journey, so arbitration is a pass-through: the source delay (a weak
constraint with an unknown reference, DUK-Q6) drives predictions, GPS progress drives actual
events. Multi-source fusion (BASE_PLAN.md section 20.6) arrives with SŽ in R2.
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
from obehy.realtime.timeline.progress import move


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
    if instance.lifecycle == "running" and position is not None:
        path = paths.get(trip, index)
        movement = move(
            path,
            instance.progress,
            instance.off_route_since,
            position,
            observation.at,
            trip.mode,
            policy,
        )
        calls = list(instance.calls)
        for call_index, kind, when in movement.crossed:
            state = calls[call_index]
            calls[call_index] = (
                replace(state, arrival=when)
                if kind == "arrival"
                else replace(state, departure=when)
            )
            effects.append(
                WriteEvent(
                    instance.journey,
                    state.location_id,
                    state.visit_n,
                    kind,
                    when,
                    "progress",
                    observation.source,
                )
            )
        finished = any(kind == "arrival" and i == len(calls) - 1 for i, kind, _ in movement.crossed)
        instance = replace(
            instance,
            calls=tuple(calls),
            progress=movement.progress,
            off_route_since=movement.off_route_since,
            off_route=movement.off_route,
            lifecycle="finished" if finished else instance.lifecycle,
        )
    return instance, effects
