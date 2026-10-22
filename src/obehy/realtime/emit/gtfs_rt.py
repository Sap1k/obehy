"""Per-feed GTFS-RT from core state (BASE_PLAN.md section 30): matched vehicles only.

Output is deterministic: entities in sorted order, timestamps from the tick clock and the
observations, never from the wall clock.

A TripUpdate does not leave the feed when realtime about the trip ends: a consumer would revert
the whole trip to the timetable, the calls already made included, and plan on top of times that
never happened. A finished journey keeps its TripUpdate (the calls it made, no vehicle) for
`lifecycle.publish_finished_s` after its last call; a lost one (nothing heard for
`predict_without_data_s`) keeps the calls it made, every call ahead `NO_DATA`, until it is
forgotten. A cancelled one is `CANCELED`, without calls or vehicle, until it is forgotten.
Vehicle positions are published for live journeys only, and not while stale.

A rail run is published per trip part (docs/R2_SLICE.md section 2): each part gets its own
TripUpdate with its own `stop_sequence`s, the junction call shared by two parts carries the
arrival in the earlier part and the departure in the later, and railway points that are not
passenger stops are never published. A platform mapped to a static boarding point (a track)
becomes `assigned_stop_id`, even on a call without predicted times; a platform that only has a
label (a big station's platform number) is not published here.

A bus reported by several sources (DÚK and a city's own feed) is published once, from the
source the tracker follows. A rail run is one train however many vehicles report it: coupled
DÚK units each have a GPS, and the SŽ map is a vehicle of its own. It gets one VehiclePosition
under an opaque vehicle ID that is stable for the run's day, labelled with the current train
number; the position is the leading vehicle's of the source fusion last tracked
(`track_source`), else any bound one's.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from google.transit import gtfs_realtime_pb2 as rt

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.emit.represent import one_source, run_position, vehicles_by_journey
from obehy.realtime.model import (
    CallState,
    FeedState,
    Instance,
    JourneyKey,
    PartSpan,
    VehicleState,
)
from obehy.realtime.policy import LifecyclePolicy
from obehy.realtime.runs import current_part, published_parts
from obehy.realtime.times import Instant

LIVE = ("pre_trip", "running")
TIMED = ("actual", "inferred", "predicted")
MADE = ("actual", "inferred")  # calls with an observed or inferred crossing

Publication = Literal["live", "lost", "finished", "cancelled"]


def _epoch(value: Instant) -> int:
    return int(value.timestamp())


def _trip(
    descriptor: rt.TripDescriptor, trip_id: str, journey: JourneyKey, *, cancelled: bool = False
) -> None:
    descriptor.trip_id = trip_id
    descriptor.start_date = journey.service_date.strftime("%Y%m%d")
    descriptor.schedule_relationship = (
        rt.TripDescriptor.CANCELED if cancelled else rt.TripDescriptor.SCHEDULED
    )


def _entity_id(instance: Instance, part: PartSpan) -> str:
    journey = instance.journey
    if not instance.parts:
        return f"trip:{journey.namespace}:{journey.key}:{journey.service_date.isoformat()}"
    return f"trip:{part.trip_id}:{journey.service_date.isoformat()}"


def _mapped(call: CallState) -> str | None:
    return call.platform.boarding_point_id if call.platform is not None else None


@lru_cache(maxsize=8192)  # bounded: the worker runs for weeks
def run_vehicle_id(journey: JourneyKey) -> str:
    """A rail run's public vehicle ID: opaque, the same for the whole run whichever unit or
    source reports it."""

    identity = f"{journey.feed}|{journey.namespace}|{journey.key}|{journey.service_date}"
    return "train-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _train_number(instance: Instance) -> str | None:
    """The train number at the run's current call (it may change along the run)."""

    reached = instance.progress.call_index if instance.progress is not None else 0
    for span in instance.journeys:
        if reached < span.last:
            return span.journey.key
    return instance.journeys[-1].journey.key if instance.journeys else None


def publication(instance: Instance, now: Instant, policy: LifecyclePolicy) -> Publication | None:
    """How a journey's TripUpdate is published at `now`, if at all."""

    if instance.lifecycle in LIVE:
        return "lost" if instance.freshness.lost else "live"
    if instance.lifecycle == "cancelled":
        return "cancelled"
    if instance.lifecycle != "finished":
        return None
    made = [
        t
        for c in instance.calls
        if c.status in MADE
        for t in (c.estimated_arrival, c.estimated_departure)
        if t is not None
    ]
    end = max(made, default=instance.freshness.updated_at)
    keep = timedelta(seconds=policy.publish_finished_s(instance.mode))
    return "finished" if now - end <= keep else None


def _vehicle_position(instance: Instance, current: VehicleState, vehicle_id: str) -> bytes:
    assert current.position is not None
    entity = rt.FeedEntity()
    entity.id = f"vehicle:{vehicle_id}"
    position = entity.vehicle
    _trip(position.trip, current_part(instance), instance.journey)
    position.vehicle.id = vehicle_id
    if instance.parts:
        label = _train_number(instance)
        if label is not None:
            position.vehicle.label = label
    position.position.latitude = current.position.lat
    position.position.longitude = current.position.lon
    if current.position.bearing is not None:
        position.position.bearing = current.position.bearing
    position.timestamp = _epoch(current.last_seen)
    return entity.SerializeToString(deterministic=True)


def _trip_updates(
    instance: Instance, vehicle_id: str | None, publication: Publication
) -> tuple[bytes, ...]:
    """The serialized TripUpdate entities of a journey, one per published part. A finished
    journey publishes the calls it made; a lost one those and `NO_DATA` for every call ahead.
    Either leaves out a part without a call made."""

    journey = instance.journey
    parts = published_parts(instance)
    if publication == "cancelled":
        return tuple(_cancelled(instance, part) for part in parts)
    timed_statuses = TIMED if publication == "live" else MADE
    out: list[bytes] = []
    for n, part in enumerate(parts):
        updates: list[tuple[CallState, bool, bool]] = []
        made = False
        for i in range(part.first, part.last + 1):
            call = instance.calls[i]
            if not call.passenger:
                continue
            timed = call.status in timed_statuses
            made = made or call.status in MADE
            if not timed and _mapped(call) is None and publication != "lost":
                continue
            # A junction shared with the neighbouring part: arrival before, departure after.
            arrival = not (n > 0 and i == part.first)
            departure = not (n < len(parts) - 1 and i == part.last)
            updates.append((call, arrival and timed, departure and timed))
        if not updates or (publication != "live" and not made):
            continue
        entity = rt.FeedEntity()
        entity.id = _entity_id(instance, part)
        update = entity.trip_update
        _trip(update.trip, part.trip_id, journey)
        if vehicle_id is not None:
            update.vehicle.id = vehicle_id
        update.timestamp = _epoch(instance.freshness.updated_at)
        for call, arrival, departure in updates:
            stop = update.stop_time_update.add()
            stop.stop_sequence = call.sequence
            if publication == "lost" and call.status not in MADE:
                stop.schedule_relationship = rt.TripUpdate.StopTimeUpdate.NO_DATA
                continue
            if arrival and call.estimated_arrival is not None:
                stop.arrival.time = _epoch(call.estimated_arrival)
            if departure and call.estimated_departure is not None:
                stop.departure.time = _epoch(call.estimated_departure)
            assigned = _mapped(call)
            if assigned is not None:
                stop.stop_time_properties.assigned_stop_id = assigned
        out.append(entity.SerializeToString(deterministic=True))
    return tuple(out)


def _cancelled(instance: Instance, part: PartSpan) -> bytes:
    entity = rt.FeedEntity()
    entity.id = _entity_id(instance, part)
    update = entity.trip_update
    _trip(update.trip, part.trip_id, instance.journey, cancelled=True)
    update.timestamp = _epoch(instance.freshness.updated_at)
    return entity.SerializeToString(deterministic=True)


def _field(number: int, payload: bytes) -> bytes:
    """A length-delimited protobuf field: the tag, the varint length, the bytes."""

    out = bytearray([number << 3 | 2])
    length = len(payload)
    while length > 0x7F:
        out.append(length & 0x7F | 0x80)
        length >>= 7
    out.append(length)
    return bytes(out) + payload


class FeedEmitter:
    """Serializes a feed's GTFS-RT from core state, keeping each entity's bytes while its
    journey and vehicle are unchanged (state objects are immutable and replaced on change, so
    identity says unchanged). The output is exactly `SerializeToString(deterministic=True)` of
    the whole message: a header field followed by entity fields in order."""

    def __init__(self, policy: LifecyclePolicy) -> None:
        self.policy = policy
        self.trips: dict[JourneyKey, tuple[Instance, str | None, tuple[bytes, ...]]] = {}
        self.positions: dict[str, tuple[Instance, VehicleState, bytes]] = {}

    def serialize(self, state: FeedState, now: Instant, alerts: Sequence[bytes] = ()) -> bytes:
        """The feed's message: its trip updates, its vehicle positions, then `alerts` (the
        feed's serialized `Alert` entities, `emit/alerts.py`)."""

        header = rt.FeedHeader()
        header.gtfs_realtime_version = "2.0"
        header.incrementality = rt.FeedHeader.FULL_DATASET
        header.timestamp = _epoch(now)
        chunks = [_field(1, header.SerializeToString(deterministic=True))]

        vehicles_of = vehicles_by_journey(state)
        for journey, vehicles in vehicles_of.items():
            instance = state.instances.get(journey)
            if instance is not None and not instance.parts:
                vehicles_of[journey] = one_source(instance, vehicles, state)

        trips: dict[JourneyKey, tuple[Instance, str | None, tuple[bytes, ...]]] = {}
        for journey, instance in sorted(state.instances.items()):
            # A stale journey keeps its predictions through a reception gap.
            published = publication(instance, now, self.policy)
            if published is None:
                continue
            vehicle_id: str | None = None  # not live: the vehicle may be on its next trip
            if published == "live" and instance.parts:
                vehicle_id = run_vehicle_id(journey)
            elif published == "live" and vehicles_of.get(journey):
                vehicle_id = str(vehicles_of[journey][0])
            cached = self.trips.get(journey)
            if cached is None or cached[0] is not instance or cached[1] != vehicle_id:
                updates = _trip_updates(instance, vehicle_id, published)
                cached = (instance, vehicle_id, updates)
            trips[journey] = cached
            chunks.extend(_field(2, entity) for entity in cached[2])
        self.trips = trips

        chosen: list[tuple[str, Instance, VehicleState]] = []
        for journey, vehicles in vehicles_of.items():
            instance = state.instances.get(journey)
            if instance is None or instance.lifecycle not in LIVE or instance.freshness.stale:
                continue
            bound = [state.vehicles[v] for v in vehicles]
            if instance.parts:
                lead = run_position(instance, bound)
                if lead is not None:
                    chosen.append((run_vehicle_id(journey), instance, lead))
                continue
            chosen.extend((str(v.vehicle), instance, v) for v in bound if v.position is not None)
        positions: dict[str, tuple[Instance, VehicleState, bytes]] = {}
        for vehicle_id, instance, current in sorted(chosen, key=lambda p: p[0]):
            hit = self.positions.get(vehicle_id)
            if hit is None or hit[0] is not instance or hit[1] is not current:
                hit = (instance, current, _vehicle_position(instance, current, vehicle_id))
            positions[vehicle_id] = hit
            chunks.append(_field(2, hit[2]))
        self.positions = positions
        chunks.extend(_field(2, entity) for entity in alerts)
        return b"".join(chunks)


def feed_message(state: FeedState, now: Instant, policy: LifecyclePolicy) -> rt.FeedMessage:
    return rt.FeedMessage.FromString(FeedEmitter(policy).serialize(state, now))


def write_feed(path: Path, payload: bytes) -> None:
    with atomic_output_path(path) as temporary:
        temporary.write_bytes(payload)
