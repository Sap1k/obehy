"""The timeline engine: progress, events and estimates of one journey instance.

GPS progress (map matching over the trip's path, `progress.py`) drives actual events, committed
once unambiguous, and the lateness the tracker measures drives predictions (`estimate.py`); the
source's own delay (a weak constraint with an unknown reference, DUK-Q6) is the fallback.
Several sources on one journey (DÚK GPS, the SŽ map, a city's feed) are fused by `fusion.py`:
per-source tracks, SŽ point events as anchors (docs/R2_SLICE.md section 5). Every source's newer
fix feeds the one tracker, weighted by its precision; one that would take the tracker back
while a better-ranked source is fresh is dropped (`Unresolved` "behind"). The journey is shown
where the tracker reads it (`Published`).
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.model import (
    CallState,
    Delay,
    Effect,
    Freshness,
    Instance,
    Observation,
    PointEvent,
    Position,
    Published,
    SourceSemantics,
    SourceState,
    Unresolved,
    WriteEvent,
)
from obehy.realtime.policy import Policy, ProgressPolicy
from obehy.realtime.timeline.commit import Crossing
from obehy.realtime.timeline.fusion import (
    apply_point,
    merge,
    note_source,
    position_leads,
    tracks_with,
    with_track,
)
from obehy.realtime.timeline.path import heading_at, point_at, project_all
from obehy.realtime.timeline.plan import Plan, PlanCache
from obehy.realtime.timeline.progress import Step, update
from obehy.realtime.times import Instant


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

    track, previous, news = note_source(instance, observation, semantics)
    if not news:
        return instance, []  # SZ-Q6: an unchanged entry says nothing new
    position = observation.first(Position)
    seen = instance.track.seen_at if instance.track is not None else None
    # A fix no newer than the last one used (repeated or out of order, or a GPS time frozen
    # while payloads keep coming) is no new information: it never moves progress, so event
    # intervals always run forward in time, and it does not keep the journey fresh.
    repeated = position is not None and seen is not None and observation.at <= seen
    freshness, delay_s = _note(instance, observation, policy, semantics, fresh=not repeated)
    # The instance is rebuilt once per fix: what the source said goes in with the rest.
    effects: list[Effect] = []
    if observation.first(PointEvent) is not None and instance.lifecycle != "finished":
        instance = with_track(instance, track).evolve(freshness=freshness, delay_s=delay_s)
        plan = plans.plan(trip, instance.journey.service_date, index, policy)
        instance, effects = apply_point(instance, trip, plan, observation, previous, index, policy)
        track = instance.source_track(observation.source) or track
    if instance.lifecycle != "running" or position is None or repeated:
        return with_track(instance, track).evolve(freshness=freshness, delay_s=delay_s), effects
    source = observation.source
    progress_policy = policy.progress
    run = trip.run_key is not None  # a CZPTT run, whatever its mode (rail-replacement bus)
    sigma_m = policy.rail.position_sigma_m(source)
    if run:
        progress_policy = _rail_progress(progress_policy, sigma_m)
    plan = plans.plan(trip, instance.journey.service_date, index, policy)
    if _behind(instance, plan, position, sigma_m) and not position_leads(
        instance, source, observation.at, policy
    ):
        # A lagging fix (the SŽ map's, a feed's late copy) while a better source is fresh.
        instance = with_track(instance, track).evolve(freshness=freshness, delay_s=delay_s)
        return instance, [*effects, Unresolved(observation, "behind", source)]
    step = update(
        plan, instance.track, position, observation.at, trip.mode, source, progress_policy
    )
    track = track.evolve(position_at=observation.at)
    calls, crossed = _cross(instance, step.crossed, source, policy, fuse=run)
    finished = any(c.kind == "arrival" and c.call == len(calls) - 1 for c in step.crossed)
    progress = step.progress
    held = instance.progress
    if run and held is not None and (progress is None or progress.distance_m < held.distance_m):
        progress = held  # a source's point anchors progress; fixes never take it back
    instance = instance.evolve(
        calls=calls,
        progress=progress,
        track=step.track,
        off_route_since=step.off_route_since,
        off_route=step.off_route,
        lifecycle="finished" if finished else instance.lifecycle,
        track_source=source,
        freshness=freshness,
        delay_s=delay_s,
        sources=tracks_with(instance.sources, track),
    )
    shown_source = _shown(instance, source, observation.at, policy)
    published = instance.published
    if source == shown_source or published is None:
        published = _published(plan, step, position, observation.at, published, policy.progress)
    instance = instance.evolve(shown_source=shown_source, published=published)
    return instance, [*effects, *crossed]


def _behind(instance: Instance, plan: Plan, position: Position, sigma_m: float) -> bool:
    """Whether the fix lies on the path behind the tracker's reading by more than its error."""

    best = instance.track.hypotheses[0] if instance.track and instance.track.hypotheses else None
    if best is None or len(plan.path.points) < 2:
        return False
    nearest = min(project_all(plan.path, position.lon, position.lat), key=lambda p: p.lateral_m)
    return nearest.along_m < best.along_m - sigma_m


def _shown(instance: Instance, source: str, at: Instant, policy: Policy) -> str:
    """The source whose vehicle stands for the journey: kept while its fixes stay fresh."""

    shown = instance.shown_source
    held = instance.source_track(shown) if shown is not None else None
    fresh = policy.rail.position_fresh_s
    if held is None or held.position_at is None or (at - held.position_at).total_seconds() > fresh:
        return source
    return held.source


def _published(
    plan: Plan,
    step: Step,
    position: Position,
    at: Instant,
    before: Published | None,
    policy: ProgressPolicy,
) -> Published:
    """Where the journey is shown after a fix of the shown source: on a real shape the
    tracker's reading, never behind the last one shown; else (stop-to-stop chords, off the
    shape) the fix itself, unless it lies further back than the jitter (a fix drawn back and
    forward again on every refresh)."""

    path = plan.path
    best = step.track.hypotheses[0] if step.track.hypotheses else None
    held = before.along_m if before is not None else None
    on_shape = path.shaped and step.track.off_path is None
    if best is not None and on_shape and best.off_path_since is None:
        along = best.along_m
        if before is not None and before.snapped and held is not None:
            along = max(along, held)
        lon, lat = point_at(path, along)
        return Published(lon, lat, heading_at(path, along), at, along, snapped=True)
    along = None
    if len(path.points) >= 2:
        nearest = min(project_all(path, position.lon, position.lat), key=lambda p: p.lateral_m)
        along = nearest.along_m
    if (
        before is not None
        and held is not None
        and along is not None
        and along < held - policy.jitter_m
    ):
        return before
    return Published(position.lon, position.lat, position.bearing, at, along)


@lru_cache(maxsize=64)
def _rail_progress(policy: ProgressPolicy, gps_sigma_m: float) -> ProgressPolicy:
    """The progress policy with a rail source's position error (one per source)."""

    return replace(policy, gps_sigma_m=gps_sigma_m)


def _note(
    instance: Instance,
    observation: Observation,
    policy: Policy,
    semantics: SourceSemantics,
    *,
    fresh: bool,
) -> tuple[Freshness, int | None]:
    """The observation's time and source delay; only new information keeps it fresh.

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
    old = instance.freshness
    heard_at = observation.received_at if fresh else old.heard_at
    return Freshness(observation.at, heard_at, old.stale, old.lost), delay_s


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

    if not crossed:
        return instance.calls, []
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
