"""Several sources on one rail run (docs/R2_SLICE.md section 5, BASE_PLAN.md section 20.6).

No source owns a run; each keeps a `SourceTrack` on the instance, and each capability is
decided on its own:

- position: the best-ranked source with a fresh fix leads (`rail.position_preference`: DÚK GPS
  before the SŽ map); a lower one's fixes feed the tracker only while no better one is fresh;
- events: a source's point event (SŽ `cna`) commits the call with its minute and anchors
  progress there; a crossing measured from positions inside that minute (widened by
  `rail.event_tolerance_s`) narrows it, one outside loses to it;
- current delay: measured lateness while the tracker follows a source without its own point
  delays, else the point delay (SŽ `de`);
- prediction: `rail.predictor` - the source's predicted change applied to the own anchor
  (`anchor_change`), the source's prediction itself (`sz`), or plain propagation.

A run is stale only when every source is silent: `Freshness.heard_at` is the latest of them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.model import (
    CallState,
    Delay,
    Effect,
    EventKind,
    Instance,
    Interval,
    Lead,
    NextPoint,
    NextStopPrediction,
    Observation,
    PointEvent,
    Progress,
    SourceClass,
    SourceSemantics,
    SourceTrack,
    Track,
    Unresolved,
    VehicleId,
    WriteEvent,
)
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.plan import Plan
from obehy.realtime.times import Instant

POINT = "point"


def _with_track(instance: Instance, track: SourceTrack) -> Instance:
    others = tuple(t for t in instance.sources if t.source != track.source)
    return replace(instance, sources=tuple(sorted((*others, track), key=lambda t: t.source)))


def lead_of(instance: Instance, source: str) -> Lead | None:
    """The vehicle leading one source's contributions (DUK-Q11 within a source)."""

    lead = instance.lead
    if lead is not None and lead.vehicle.source == source:
        return lead
    track = instance.source_track(source)
    return None if track is None else track.lead


def with_lead(instance: Instance, vehicle: VehicleId, observation: Observation) -> Instance:
    lead = Lead(vehicle, observation.at)
    if instance.lead is None or instance.lead.vehicle.source == vehicle.source:
        return replace(instance, lead=lead)
    track = instance.source_track(vehicle.source) or SourceTrack(
        vehicle.source, observation.received_at
    )
    return _with_track(instance, replace(track, lead=lead))


def note_source(
    instance: Instance, observation: Observation, semantics: SourceSemantics
) -> tuple[Instance, SourceTrack | None, bool]:
    """Record what the source says; returns the instance, the source's previous track, and
    False for an unchanged entry of a source where that is no news (SZ-Q6)."""

    previous = instance.source_track(observation.source)
    fingerprint = hash(observation.facts) if semantics.unchanged_entry_is_no_news else None
    if fingerprint is not None and previous is not None and previous.fingerprint == fingerprint:
        return instance, previous, False
    track = previous or SourceTrack(observation.source, observation.received_at)
    delay = observation.first(Delay)
    if delay is not None:
        track = replace(track, delay_s=delay.seconds, delay_at=observation.at)
        if delay.reference == POINT:
            track = replace(track, delay_reference=POINT)
    track = replace(
        track,
        heard_at=observation.received_at,
        fingerprint=fingerprint,
        next_point=observation.first(NextPoint) or track.next_point,
        prediction=observation.first(NextStopPrediction) or track.prediction,
    )
    return _with_track(instance, track), previous, True


def position_leads(instance: Instance, source: str, at: Instant, policy: Policy) -> bool:
    """Whether this source's fix may move the tracker: no better-ranked source is fresh."""

    rail = policy.rail
    rank = rail.rank(source)
    fresh = timedelta(seconds=rail.position_fresh_s)
    for other in instance.sources:
        if other.source == source or other.position_at is None:
            continue
        if rail.rank(other.source) < rank and at - other.position_at <= fresh:
            return False
    return True


def note_position(instance: Instance, source: str, at: Instant) -> Instance:
    track = instance.source_track(source) or SourceTrack(source, at)
    return _with_track(instance, replace(track, position_at=at))


def merge(
    existing: Interval | None, new: Interval, *, new_is_point: bool, policy: Policy
) -> Interval:
    """One event from two: a measured interval inside the point's minute (widened by the
    tolerance) narrows it; one outside loses to the point."""

    if existing is None:
        return new
    point, measured = (new, existing) if new_is_point else (existing, new)
    slack = timedelta(seconds=policy.rail.event_tolerance_s)
    lo = max(measured.lo, Instant(point.lo - slack))
    hi = min(measured.hi, Instant(point.hi + slack))
    return Interval(lo, hi) if lo <= hi else point


def _middle(interval: Interval) -> Instant:
    return Instant(interval.lo + (interval.hi - interval.lo) / 2)


def _frontier(instance: Instance) -> int:
    """The first call a new point may be: the last one with any realtime, or progress."""

    reached = instance.progress.call_index if instance.progress is not None else 0
    for i, call in enumerate(instance.calls):
        if call.arrival or call.departure or call.passed_arrival or call.passed_departure:
            reached = max(reached, i)
    return max(reached, 0)


def _start(instance: Instance, previous: SourceTrack | None) -> int:
    """Where a source's point may lie: from the call it placed the train at last (it repeats a
    point while other fields of its entry change), else one call behind the run's frontier (a
    source reports a point another one has just passed)."""

    if previous is not None and previous.point is not None:
        return previous.point
    return max(0, _frontier(instance) - 1)


def _resolve(
    instance: Instance,
    trip: Trip,
    start: int,
    codes: tuple[str, ...],
    actual: Interval,
    index: IndexView,
    policy: Policy,
) -> tuple[int | None, bool]:
    """The first call from `start` with one of the codes that fits the run's other events in
    time; with codes found but none fitting, (None, True)."""

    found = False
    for i in range(start, len(trip.calls)):
        if index.location_key(trip.calls[i].location_id, "sr70") not in codes:
            continue
        found = True
        if _consistent(instance, i, actual, policy):
            return i, False
    return None, found


def _consistent(instance: Instance, i: int, actual: Interval, policy: Policy) -> bool:
    """Whether a point event at call `i` fits the run's other events in time (within the
    tolerance): never before an earlier call's, never after a later call's. A stale report, or
    a station the run passes twice, otherwise lands on the wrong call."""

    slack = timedelta(seconds=policy.rail.event_tolerance_s)
    for j, call in enumerate(instance.calls):
        times = [t for t in (call.passed_arrival, call.passed_departure) if t is not None]
        if not times or j == i:
            continue
        if j < i and actual.hi + slack < max(times):
            return False
        if j > i and actual.lo - slack > min(times):
            return False
    return True


def anchor(track: Track | None, along_m: float, policy: Policy) -> Track | None:
    """Readings behind a source's point contradict it and are dropped; nothing before it is
    committed again."""

    if track is None:
        return None
    jitter = policy.progress.jitter_m
    kept = tuple(h for h in track.hypotheses if h.along_m >= along_m - jitter)
    return replace(track, hypotheses=kept, committed_m=max(track.committed_m, along_m))


def apply_point(
    instance: Instance,
    trip: Trip,
    plan: Plan,
    observation: Observation,
    previous: SourceTrack | None,
    index: IndexView,
    policy: Policy,
) -> tuple[Instance, list[Effect]]:
    """A point event: the call it names at or after the frontier gets its event (SZ-Q3: the
    arrival while standing, else the departure, or a passage at a railway point)."""

    event = observation.first(PointEvent)
    if event is None:
        return instance, []
    codes = event.codes
    hint = previous.next_point if previous is not None else None
    if hint is not None and hint.name == event.name and hint.sr70 is not None:
        codes = (*codes, hint.sr70)
    i, misfit = _resolve(
        instance, trip, _start(instance, previous), codes, event.actual, index, policy
    )
    if i is None:
        why = "inconsistent" if misfit else "point"
        return instance, [Unresolved(observation, why, event.name)]  # moves nothing
    last = len(trip.calls) - 1
    if i == 0 and event.standing:
        return instance, []  # waiting at the origin: nothing has happened yet
    call = trip.calls[i]
    kind: EventKind
    if event.standing:
        kind = "arrival"
    elif call.passenger_service or i in (0, last):
        kind = "departure"
    else:
        kind = "passage"
    calls = list(instance.calls)
    state: CallState = calls[i]
    before = state.arrival if kind == "arrival" else state.departure
    if kind == "arrival":
        merged = merge(state.arrival, event.actual, new_is_point=True, policy=policy)
        state = replace(state, arrival=merged, passed_arrival=_middle(merged))
    else:
        merged = merge(state.departure, event.actual, new_is_point=True, policy=policy)
        state = replace(state, departure=merged, passed_departure=_middle(merged))
        if kind == "passage" and state.arrival is None:
            state = replace(state, passed_arrival=_middle(merged))
    calls[i] = state
    zone = plan.zones[i]
    along = zone.arrival_m if kind == "arrival" else zone.departure_m
    if along is None:
        along = plan.path.call_distances_m[i]
    progress = instance.progress
    if progress is None or along > progress.distance_m:
        progress = Progress(along, plan.call_index_at(along), event.actual.lo)
    lifecycle = instance.lifecycle
    if lifecycle == "pre_trip":
        lifecycle = "running"
    if kind == "arrival" and i == last:
        lifecycle = "finished"
    track = instance.source_track(observation.source)
    instance = replace(
        instance,
        calls=tuple(calls),
        progress=progress,
        track=anchor(instance.track, along, policy),
        lifecycle=lifecycle,
    )
    if track is not None:
        instance = _with_track(instance, replace(track, point=i))
    if merged == before:
        return instance, []  # a repeat of the point already recorded
    journey, visit = instance.public_call(i, kind)
    effect = WriteEvent(
        journey, call.location_id, visit, kind, merged, "source", observation.source
    )
    return instance, [effect]


# --- delay and prediction ----------------------------------------------------------------------


def current_lateness(instance: Instance) -> tuple[timedelta | None, SourceClass | None]:
    """Measured lateness while the tracker follows a source without its own point delays,
    else the freshest point delay; else R1's rule (tracker, then the source's delay)."""

    point = max(
        (t for t in instance.sources if t.delay_reference == POINT and t.delay_s is not None),
        key=lambda t: t.delay_at or t.heard_at,
        default=None,
    )
    track = instance.track
    measured = track is not None and bool(track.hypotheses)
    by_point = instance.track_source is not None and any(
        t.source == instance.track_source and t.delay_reference == POINT for t in instance.sources
    )
    if measured and not (by_point and point is not None):
        assert track is not None
        return timedelta(seconds=round(track.hypotheses[0].lateness_s)), "gps"
    if point is not None and point.delay_s is not None:
        return timedelta(seconds=point.delay_s), "source"
    if instance.delay_s is not None:
        return timedelta(seconds=instance.delay_s), "source"
    return None, None


def anchor_prediction(
    instance: Instance, trip: Trip, index: IndexView, predictor: str
) -> tuple[int, timedelta] | None:
    """The lateness at the source's predicted next stop, by `predictor`; None for plain
    propagation or without a usable source prediction."""

    if predictor == "propagate":
        return None
    source = max(
        (t for t in instance.sources if t.prediction is not None),
        key=lambda t: t.heard_at,
        default=None,
    )
    if source is None or source.prediction is None:
        return None
    prediction = source.prediction
    if prediction.scheduled is None or prediction.predicted is None:
        return None
    reached = instance.progress.call_index if instance.progress is not None else -1
    target = next(
        (
            i
            for i in range(max(reached + 1, 0), len(trip.calls))
            if trip.calls[i].passenger_service
            and index.location_key(trip.calls[i].location_id, "sr70") == prediction.sr70
        ),
        None,
    )
    if target is None:
        return None
    predicted_delay = prediction.predicted - prediction.scheduled
    if predictor == "sz":
        return target, predicted_delay
    own, _ = current_lateness(instance)
    if own is None or source.delay_s is None:
        return target, predicted_delay
    return target, own + (predicted_delay - timedelta(seconds=source.delay_s))
