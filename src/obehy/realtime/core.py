"""`step`: one observation through the realtime core (BASE_PLAN.md sections 19-20).

Pure: no database, network or files. The worker and replay run the same function, load the
index for the observation's keys before calling it, and execute the returned effects.

Binding continuity (section 19.5): an observation with the key of its current binding stays on
that journey, and its service date is never re-derived, until the time leaves the journey's
admissible window. Then the binding ends, and only a new admissible journey for the key binds
again (DUK-Q4: yesterday's key in the morning is `not_in_service`, never an extended trip).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta

from obehy.realtime import timeline
from obehy.realtime.index import IndexView, Trip
from obehy.realtime.infer.keyed import Match, bind, in_window, span
from obehy.realtime.model import (
    AssignVehicle,
    Binding,
    CallState,
    Effect,
    FeedState,
    Instance,
    JourneyKey,
    Observation,
    ObservationResult,
    Position,
    Reason,
    ScheduledCall,
    SnapshotJourney,
    SourceState,
    TripKey,
    VehicleId,
    VehicleKey,
    VehicleState,
    VehicleStatus,
)
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.estimate import estimate
from obehy.realtime.timeline.path import PathCache
from obehy.realtime.times import Instant

CORE_VERSION = "r1.0"

# Normalized source states; connectors map their own codes onto these.
SOURCE_PRE_TRIP = "pre_trip"  # DUK-Q5: no progress or events until it departs
SOURCE_RUNNING = "running"


@dataclass(frozen=True, slots=True)
class Context:
    index: IndexView
    policy: Policy
    paths: PathCache = field(default_factory=PathCache)


def vehicle_of(observation: Observation) -> VehicleId | None:
    key = observation.first(VehicleKey)
    return None if key is None else VehicleId(observation.source, key.source_vehicle_id)


def step(state: FeedState, observation: Observation, ctx: Context) -> list[Effect]:
    """Apply one observation to `state` (updated in place) and return its effects."""

    if observation.feed != state.feed:
        raise ValueError(f"observation for {observation.feed} applied to {state.feed} state")
    effects: list[Effect] = []
    vehicle = vehicle_of(observation)
    key = observation.first(TripKey)
    if vehicle is None or key is None:
        effects.append(ObservationResult(observation, None, Reason.NO_KEY))
        if vehicle is not None:
            _set_vehicle(state, observation, vehicle, "unmatched", None, None, Reason.NO_KEY)
        return effects

    previous = state.vehicles.get(vehicle)
    binding = _continued(previous, key, observation, state, ctx)
    if binding is None:
        result = bind(key, observation.at, ctx.index, ctx.policy)
        if not isinstance(result, Match):
            status: VehicleStatus = (
                "not_in_service" if result is Reason.NOT_IN_SERVICE else "unmatched"
            )
            _set_vehicle(state, observation, vehicle, status, key, None, result)
            effects.append(ObservationResult(observation, None, result))
            return effects
        binding = Binding(vehicle, result.journey, result.trip.trip_id, "keyed", observation.at)
        effects.extend(_open(state, result, ctx, observation))
        effects.append(AssignVehicle(vehicle, result.journey, observation.at))

    trip = ctx.index.trip(binding.trip_id)
    instance = state.instances[binding.journey]
    if _leads(instance, vehicle, observation, ctx.policy):
        instance = replace(instance, lead=vehicle, lead_seen=observation.at)
        instance = _lifecycle(instance, observation, span(trip, binding.journey)[0])
        instance, timeline_effects = timeline.advance(
            instance, trip, observation, ctx.index, ctx.policy, ctx.paths
        )
        state.instances[binding.journey] = instance
        state.dirty.add(binding.journey)
        effects.extend(timeline_effects)
    status = "positioning" if instance.lifecycle in ("forecast", "pre_trip") else "running"
    if instance.lifecycle == "finished":
        status = "layover"
    _set_vehicle(state, observation, vehicle, status, key, binding, None)
    effects.append(ObservationResult(observation, binding.journey, None))
    return effects


def estimate_all(state: FeedState, ctx: Context) -> None:
    """Bring per-call estimates of changed journeys up to date (before emitting state)."""

    for journey in sorted(state.dirty):
        instance = state.instances.get(journey)
        if instance is None:
            continue
        trip = ctx.index.trip(instance.trip_id)
        state.instances[journey] = estimate(
            instance, ctx.paths.scheduled(trip, journey.service_date)
        )
    state.dirty.clear()


def refresh(state: FeedState, now: Instant, policy: Policy) -> None:
    """Emit-tick housekeeping: flag stale journeys, forget long-unseen vehicles and journeys."""

    stale_after = timedelta(seconds=policy.lifecycle.stale_after_s)
    forget = timedelta(seconds=policy.lifecycle.forget_after_s)
    for vehicle, current in sorted(state.vehicles.items()):
        if now - current.last_seen > forget:
            del state.vehicles[vehicle]
    bound = {v.binding.journey for v in state.vehicles.values() if v.binding is not None}
    for journey, instance in sorted(state.instances.items()):
        idle = now - instance.updated_at
        if idle > forget and journey not in bound:
            del state.instances[journey]
            continue
        stale = instance.lifecycle != "finished" and idle > stale_after
        if stale != instance.stale:
            state.instances[journey] = replace(instance, stale=stale)


def _leads(
    instance: Instance, vehicle: VehicleId, observation: Observation, policy: Policy
) -> bool:
    """Whether this vehicle drives the journey's timeline: it is the lead, there is none, or the
    lead has gone stale. One timeline never mixes two vehicles' fixes (DUK-Q11)."""

    if instance.lead is None or instance.lead == vehicle or instance.lead_seen is None:
        return True
    return observation.at - instance.lead_seen > timedelta(seconds=policy.lifecycle.stale_after_s)


def _continued(
    previous: VehicleState | None,
    key: TripKey,
    observation: Observation,
    state: FeedState,
    ctx: Context,
) -> Binding | None:
    if previous is None or previous.binding is None or previous.trip_key != key:
        return None
    binding = previous.binding
    instance = state.instances.get(binding.journey)
    if instance is None:
        return None
    trip = ctx.index.trip(binding.trip_id)
    match = Match(binding.journey, trip, *span(trip, binding.journey))
    return binding if in_window(match, observation.at, ctx.policy) else None


def _open(state: FeedState, match: Match, ctx: Context, observation: Observation) -> list[Effect]:
    """Create the journey's instance on first binding, with its schedule snapshot."""

    if match.journey in state.instances:
        return []  # a second vehicle on the same journey (DUK-Q11)
    trip = match.trip
    state.instances[match.journey] = Instance(
        journey=match.journey,
        release_id=ctx.index.release_id,
        trip_id=trip.trip_id,
        lifecycle="pre_trip",
        calls=tuple(CallState(c.sequence, c.location_id, c.visit_n) for c in trip.calls),
        updated_at=observation.at,
    )
    return [snapshot(match.journey, ctx.index, trip.trip_id, observation.at)]


def snapshot(journey: JourneyKey, index: IndexView, trip_id: str, at: Instant) -> SnapshotJourney:
    trip = index.trip(trip_id)
    return SnapshotJourney(
        journey=journey,
        at=at,
        release_id=index.release_id,
        trip_id=trip.trip_id,
        route_name=trip.route_name,
        headsign=trip.headsign,
        calls=tuple(
            ScheduledCall(
                ordinal,
                call.location_id,
                call.visit_n,
                call.passenger_service,
                call.arrival,
                call.departure,
                index.location(call.location_id).name,
            )
            for ordinal, call in enumerate(trip.calls, start=1)
        ),
    )


def _lifecycle(instance: Instance, observation: Observation, start: Instant) -> Instance:
    """`pre_trip` becomes `running` when the source says so, or, for a source without trip
    states, once the scheduled start has passed. A pre-trip source state (DUK-Q5) holds it."""

    if instance.lifecycle != "pre_trip":
        return instance
    source = observation.first(SourceState)
    started = source.code == SOURCE_RUNNING if source is not None else observation.at >= start
    return replace(instance, lifecycle="running") if started else instance


def _set_vehicle(
    state: FeedState,
    observation: Observation,
    vehicle: VehicleId,
    status: VehicleStatus,
    key: TripKey | None,
    binding: Binding | None,
    reason: Reason | None,
) -> None:
    previous = state.vehicles.get(vehicle)
    position = observation.first(Position)
    if position is None and previous is not None:
        position = previous.position
    state.vehicles[vehicle] = VehicleState(
        vehicle=vehicle,
        feed=state.feed,
        status=status,
        last_seen=observation.at,
        trip_key=key,
        binding=binding,
        position=position,
        reason=reason,
    )


def rebase(state: FeedState, index: IndexView) -> list[Effect]:
    """Move live journeys onto a newly activated release (BASE_PLAN.md section 29).

    Each journey is re-resolved through its own key and service date and re-snapshotted, which
    history records as a new revision. Calls keep their observed events by
    `(location_id, visit_n)`; progress restarts from the next fix because path distances
    change. A journey whose key no longer runs is dropped and its vehicles unbound. The caller
    has loaded every live journey's key into `index`.
    """

    effects: list[Effect] = []
    moved: dict[JourneyKey, str] = {}
    for journey, instance in sorted(state.instances.items()):
        trip = _resolve(journey, index)
        if trip is None:
            del state.instances[journey]
            continue
        old = {(c.location_id, c.visit_n): c for c in instance.calls}
        calls = tuple(
            replace(
                old.get(
                    (c.location_id, c.visit_n), CallState(c.sequence, c.location_id, c.visit_n)
                ),
                sequence=c.sequence,
            )
            for c in trip.calls
        )
        state.instances[journey] = replace(
            instance,
            release_id=index.release_id,
            trip_id=trip.trip_id,
            calls=calls,
            progress=None,
        )
        moved[journey] = trip.trip_id
        state.dirty.add(journey)
        effects.append(snapshot(journey, index, trip.trip_id, instance.updated_at))
    for vehicle, current in sorted(state.vehicles.items()):
        binding = current.binding
        if binding is None:
            continue
        if binding.journey in moved:
            state.vehicles[vehicle] = replace(
                current, binding=replace(binding, trip_id=moved[binding.journey])
            )
        else:
            state.vehicles[vehicle] = replace(
                current, binding=None, status="unmatched", reason=Reason.NO_TRIP
            )
    return effects


def _resolve(journey: JourneyKey, index: IndexView) -> Trip | None:
    for entry in index.keys(journey.namespace, journey.key):
        if not entry.valid_on(journey.service_date):
            continue
        trip = index.trip(entry.public_id)
        if index.runs_on(trip.service_id, journey.service_date):
            return trip
    return None
