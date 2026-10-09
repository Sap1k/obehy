"""The timeline engine: progress, events and estimates of one journey instance.

R1 has one source per journey. GPS progress (map matching over the trip's path, `progress.py`)
drives actual events, committed once unambiguous, and the lateness the tracker measures drives
predictions (`estimate.py`); the source's own delay (a weak constraint with an unknown
reference, DUK-Q6) is the fallback. Multi-source fusion (BASE_PLAN.md section 20.6) arrives
with SŽ in R2.
"""

from __future__ import annotations

from dataclasses import replace

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.model import (
    CallState,
    Delay,
    Effect,
    Instance,
    Observation,
    Position,
    SourceState,
    WriteEvent,
)
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.commit import Crossing
from obehy.realtime.timeline.plan import PlanCache
from obehy.realtime.timeline.progress import update


def advance(
    instance: Instance,
    trip: Trip,
    observation: Observation,
    index: IndexView,
    policy: Policy,
    plans: PlanCache,
) -> tuple[Instance, list[Effect]]:
    """Apply one bound observation to its instance."""

    position = observation.first(Position)
    seen = instance.track.seen_at if instance.track is not None else None
    # A fix no newer than the last one used (repeated or out of order, or a GPS time frozen
    # while payloads keep coming) is no new information: it never moves progress, so event
    # intervals always run forward in time, and it does not keep the journey fresh.
    repeated = position is not None and seen is not None and observation.at <= seen
    instance = _note(instance, observation, policy, fresh=not repeated)
    if instance.lifecycle != "running" or position is None or repeated:
        return instance, []

    plan = plans.plan(trip, instance.journey.service_date, index, policy)
    step = update(plan, instance.track, position, observation.at, trip.mode, policy.progress)
    calls, effects = _cross(instance, step.crossed, observation.source)
    finished = any(c.kind == "arrival" and c.call == len(calls) - 1 for c in step.crossed)
    instance = replace(
        instance,
        calls=calls,
        progress=step.progress,
        track=step.track,
        off_route_since=step.off_route_since,
        off_route=step.off_route,
        lifecycle="finished" if finished else instance.lifecycle,
    )
    return instance, effects


def _note(instance: Instance, observation: Observation, policy: Policy, *, fresh: bool) -> Instance:
    """Record the observation's time and source delay; only new information keeps it fresh.

    Before departure, or while the source still says so (DUK-Q18), the source delay is ignored:
    DÚK reports the time since the scheduled departure there, growing while the vehicle stands
    in the depot (DUK-Q14)."""

    delay = observation.first(Delay)
    source = observation.first(SourceState)
    before = instance.lifecycle == "pre_trip" or (source is not None and source.code == "pre_trip")
    delay_s = instance.delay_s
    if delay is not None and not before and delay.seconds > policy.delay_discard_below_s:
        delay_s = delay.seconds
    freshness = replace(instance.freshness, updated_at=observation.at)
    if fresh:
        freshness = replace(freshness, heard_at=observation.received_at)
    return replace(instance, freshness=freshness, delay_s=delay_s)


def _cross(
    instance: Instance, crossed: tuple[Crossing, ...], source: str
) -> tuple[tuple[CallState, ...], list[Effect]]:
    """Set the calls' passed times and recorded events; recorded ones go to history."""

    calls = list(instance.calls)
    effects: list[Effect] = []
    for crossing in crossed:
        state = calls[crossing.call]
        if crossing.kind == "arrival":
            state = replace(state, passed_arrival=crossing.at)
            if crossing.recorded:
                state = replace(state, arrival=crossing.fixes)
        else:
            state = replace(state, passed_departure=crossing.at)
            if crossing.recorded:
                state = replace(state, departure=crossing.fixes)
        calls[crossing.call] = state
        if crossing.recorded:  # one passed inside a reception gap is realtime only
            effects.append(
                WriteEvent(
                    instance.journey,
                    state.location_id,
                    state.visit_n,
                    crossing.kind,
                    crossing.fixes,
                    "progress",
                    source,
                )
            )
    return tuple(calls), effects
