"""Per-feed GTFS-RT from core state (BASE_PLAN.md section 30): matched vehicles only.

Output is deterministic: entities in sorted order, timestamps from the tick clock and the
observations, never from the wall clock. Stale and finished journeys are left out.

A rail run is published per trip part (docs/R2_SLICE.md section 2): each part gets its own
TripUpdate with its own `stop_sequence`s, the junction call shared by two parts carries the
arrival in the earlier part and the departure in the later, and railway points that are not
passenger stops are never published. A platform mapped to a static boarding point (a track)
becomes `assigned_stop_id`, even on a call without predicted times; a platform that only has a
label (a big station's platform number) is not published here.

A rail run is one train however many vehicles report it: coupled DÚK units each have a GPS,
and the SŽ map is a vehicle of its own. It gets one VehiclePosition under an opaque vehicle ID
that is stable for the run's day, labelled with the current train number; the position is the
leading vehicle's of the source fusion last tracked (`track_source`), else any bound one's.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as rt

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.model import (
    CallState,
    FeedState,
    Instance,
    JourneyKey,
    PartSpan,
    VehicleId,
    VehicleState,
)
from obehy.realtime.times import Instant

LIVE = ("pre_trip", "running")
TIMED = ("actual", "inferred", "predicted")


def _epoch(value: Instant) -> int:
    return int(value.timestamp())


def _trip(descriptor: rt.TripDescriptor, trip_id: str, journey: JourneyKey) -> None:
    descriptor.trip_id = trip_id
    descriptor.start_date = journey.service_date.strftime("%Y%m%d")
    descriptor.schedule_relationship = rt.TripDescriptor.SCHEDULED


def _parts(instance: Instance) -> tuple[PartSpan, ...]:
    return instance.parts or (PartSpan(instance.trip_id, 0, len(instance.calls) - 1),)


def _entity_id(instance: Instance, part: PartSpan) -> str:
    journey = instance.journey
    if not instance.parts:
        return f"trip:{journey.namespace}:{journey.key}:{journey.service_date.isoformat()}"
    return f"trip:{part.trip_id}:{journey.service_date.isoformat()}"


def _mapped(call: CallState) -> str | None:
    return call.platform.boarding_point_id if call.platform is not None else None


def _current_part(instance: Instance) -> str:
    """The trip part the vehicle is on: the one holding the next call to reach."""

    parts = _parts(instance)
    reached = instance.progress.call_index if instance.progress is not None else -1
    for part in parts:
        if reached < part.last:
            return part.trip_id
    return parts[-1].trip_id


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


def _run_position(instance: Instance, bound: list[VehicleState]) -> VehicleState | None:
    """The vehicle whose position stands for the run: the lead of the source fusion tracked
    last, else that source's first vehicle, else the first with a position."""

    placed = [v for v in bound if v.position is not None]
    source = instance.track_source
    if source is not None:
        track = instance.source_track(source)
        lead = instance.lead
        if lead is None or lead.vehicle.source != source:
            lead = track.lead if track is not None else None
        for candidate in placed:
            if lead is not None and candidate.vehicle == lead.vehicle:
                return candidate
        for candidate in placed:
            if candidate.vehicle.source == source:
                return candidate
    return placed[0] if placed else None


def _vehicle_position(instance: Instance, current: VehicleState, vehicle_id: str) -> bytes:
    assert current.position is not None
    entity = rt.FeedEntity()
    entity.id = f"vehicle:{vehicle_id}"
    position = entity.vehicle
    _trip(position.trip, _current_part(instance), instance.journey)
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


def _trip_updates(instance: Instance, vehicle_id: str | None) -> tuple[bytes, ...]:
    """The serialized TripUpdate entities of a live journey, one per published part."""

    journey = instance.journey
    parts = _parts(instance)
    out: list[bytes] = []
    for n, part in enumerate(parts):
        updates: list[tuple[CallState, bool, bool]] = []
        for i in range(part.first, part.last + 1):
            call = instance.calls[i]
            if not call.passenger:
                continue
            timed = call.status in TIMED
            if not timed and _mapped(call) is None:
                continue
            # A junction shared with the neighbouring part: arrival before, departure after.
            arrival = not (n > 0 and i == part.first)
            departure = not (n < len(parts) - 1 and i == part.last)
            updates.append((call, arrival and timed, departure and timed))
        if not updates:
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
            if arrival and call.estimated_arrival is not None:
                stop.arrival.time = _epoch(call.estimated_arrival)
            if departure and call.estimated_departure is not None:
                stop.departure.time = _epoch(call.estimated_departure)
            assigned = _mapped(call)
            if assigned is not None:
                stop.stop_time_properties.assigned_stop_id = assigned
        out.append(entity.SerializeToString(deterministic=True))
    return tuple(out)


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

    def __init__(self) -> None:
        self.trips: dict[JourneyKey, tuple[Instance, str | None, tuple[bytes, ...]]] = {}
        self.positions: dict[str, tuple[Instance, VehicleState, bytes]] = {}

    def serialize(self, state: FeedState, now: Instant) -> bytes:
        header = rt.FeedHeader()
        header.gtfs_realtime_version = "2.0"
        header.incrementality = rt.FeedHeader.FULL_DATASET
        header.timestamp = _epoch(now)
        chunks = [_field(1, header.SerializeToString(deterministic=True))]

        vehicles_of: dict[JourneyKey, list[VehicleId]] = defaultdict(list)
        for vehicle, current in sorted(state.vehicles.items()):
            if current.binding is not None:
                vehicles_of[current.binding.journey].append(vehicle)

        trips: dict[JourneyKey, tuple[Instance, str | None, tuple[bytes, ...]]] = {}
        for journey, instance in sorted(state.instances.items()):
            if instance.lifecycle not in LIVE or instance.freshness.lost:
                continue  # a stale journey keeps its predictions through a reception gap
            if instance.parts:
                vehicle_id: str | None = run_vehicle_id(journey)
            elif vehicles_of.get(journey):
                vehicle_id = str(vehicles_of[journey][0])
            else:
                vehicle_id = None
            cached = self.trips.get(journey)
            if cached is None or cached[0] is not instance or cached[1] != vehicle_id:
                cached = (instance, vehicle_id, _trip_updates(instance, vehicle_id))
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
                lead = _run_position(instance, bound)
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
        return b"".join(chunks)


def feed_message(state: FeedState, now: Instant) -> rt.FeedMessage:
    return rt.FeedMessage.FromString(FeedEmitter().serialize(state, now))


def write_feed(path: Path, payload: bytes) -> None:
    with atomic_output_path(path) as temporary:
        temporary.write_bytes(payload)
