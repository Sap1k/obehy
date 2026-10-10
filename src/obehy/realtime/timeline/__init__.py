"""The timeline engine: progress, events and estimates of one journey instance.

GPS progress (map matching over the trip's path, `progress.py`) drives actual events, committed
once unambiguous, and the lateness the tracker measures drives predictions (`estimate.py`); the
source's own delay (a weak constraint with an unknown reference, DUK-Q6) is the fallback.
Several sources on one rail run (DÚK GPS, the SŽ map) are fused by `fusion.py`: per-source
tracks, the position lead, SŽ point events as anchors (docs/R2_SLICE.md section 5).
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
    PointEvent,
    Position,
    SourceSemantics,
    SourceState,
    WriteEvent,
)
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.commit import Crossing
from obehy.realtime.timeline.fusion import (
    apply_point,
    merge,
    note_position,
    note_source,
    position_leads,
)
from obehy.realtime.timeline.plan import PlanCache
from obehy.realtime.timeline.progress import update


def advance(
    instance: Instance,
    trip: Trip,
    observation: Observation,
    index: IndexView,
    policy: Policy,
    plans: PlanCache,
    semantics: SourceSemantics,
) -> tuple[Instance, list[Effect]]:
    """Apply one bound observation to its instance."""

    instance, previous, news = note_source(instance, observation, semantics)
    if not news:
        return instance, []  # SZ-Q6: an unchanged entry says nothing new
    position = observation.first(Position)
    seen = instance.track.seen_at if instance.track is not None else None
    # A fix no newer than the last one used (repeated or out of order, or a GPS time frozen
    # while payloads keep coming) is no new information: it never moves progress, so event
    # intervals always run forward in time, and it does not keep the journey fresh.
    repeated = position is not None and seen is not None and observation.at <= seen
    instance = _note(instance, observation, policy, semantics, fresh=not repeated)
    effects: list[Effect] = []
    if observation.first(PointEvent) is not None and instance.lifecycle != "finished":
        plan = plans.plan(trip, instance.journey.service_date, index, policy)
        instance, effects = apply_point(instance, trip, plan, observation, previous, index, policy)
    if instance.lifecycle != "running" or position is None or repeated:
        return instance, effects
    source = observation.source
    if not position_leads(instance, source, observation.at, policy):
        return instance, effects  # a better source's fix is fresh: it leads the position
    instance = note_position(instance, source, observation.at)

    progress_policy = policy.progress
    run = trip.run_key is not None  # a CZPTT run, whatever its mode (rail-replacement bus)
    if run:
        progress_policy = replace(progress_policy, gps_sigma_m=policy.rail.position_sigma_m(source))
    plan = plans.plan(trip, instance.journey.service_date, index, policy)
    step = update(plan, instance.track, position, observation.at, trip.mode, progress_policy)
    calls, crossed = _cross(instance, step.crossed, source, policy, fuse=run)
    finished = any(c.kind == "arrival" and c.call == len(calls) - 1 for c in step.crossed)
    progress = step.progress
    held = instance.progress
    if run and held is not None and (progress is None or progress.distance_m < held.distance_m):
        progress = held  # a source's point anchors progress; fixes never take it back
    instance = replace(
        instance,
        calls=calls,
        progress=progress,
        track=step.track,
        off_route_since=step.off_route_since,
        off_route=step.off_route,
        lifecycle="finished" if finished else instance.lifecycle,
        track_source=source,
    )
    return instance, [*effects, *crossed]


def _note(
    instance: Instance,
    observation: Observation,
    policy: Policy,
    semantics: SourceSemantics,
    *,
    fresh: bool,
) -> Instance:
    """Record the observation's time and source delay; only new information keeps it fresh.

    A source whose pre-trip delay is the time since the scheduled departure (DÚK: growing while
    the vehicle stands in the depot, DUK-Q14) has it ignored before departure and while it still
    reports a pre-trip state (DUK-Q18)."""

    delay = observation.first(Delay)
    source = observation.first(SourceState)
    before = instance.lifecycle == "pre_trip" or (source is not None and source.code == "pre_trip")
    elapsed = semantics.pre_trip_delay_is_elapsed and before
    delay_s = instance.delay_s
    if delay is not None and not elapsed and delay.seconds > policy.delay_discard_below_s:
        delay_s = delay.seconds
    freshness = replace(instance.freshness, updated_at=observation.at)
    if fresh:
        freshness = replace(freshness, heard_at=observation.received_at)
    return replace(instance, freshness=freshness, delay_s=delay_s)


def _cross(
    instance: Instance,
    crossed: tuple[Crossing, ...],
    source: str,
    policy: Policy,
    *,
    fuse: bool,
) -> tuple[tuple[CallState, ...], list[Effect]]:
    """Set the calls' passed times and recorded events; recorded ones go to history. A call a
    source's point event already holds keeps that event, narrowed by the crossing if they
    agree (`fusion.merge`)."""

    calls = list(instance.calls)
    effects: list[Effect] = []
    for crossing in crossed:
        state = calls[crossing.call]
        fixes = crossing.fixes
        if crossing.kind == "arrival":
            if fuse and state.arrival is not None:
                fixes = merge(state.arrival, fixes, new_is_point=False, policy=policy)
            state = replace(state, passed_arrival=crossing.at)
            if crossing.recorded:
                state = replace(state, arrival=fixes)
        else:
            if fuse and state.departure is not None:
                fixes = merge(state.departure, fixes, new_is_point=False, policy=policy)
            state = replace(state, passed_departure=crossing.at)
            if crossing.recorded:
                state = replace(state, departure=fixes)
        calls[crossing.call] = state
        if crossing.recorded:  # one passed inside a reception gap is realtime only
            journey, visit = instance.public_call(crossing.call, crossing.kind)
            effects.append(
                WriteEvent(
                    journey,
                    state.location_id,
                    visit,
                    crossing.kind,
                    fixes,
                    "progress",
                    source,
                )
            )
    return tuple(calls), effects
