"""Per-feed GTFS-RT from core state (BASE_PLAN.md section 30): matched vehicles only.

Output is deterministic: entities in sorted order, timestamps from the tick clock and the
observations, never from the wall clock. Stale and finished journeys are left out.

A rail run is published per trip part (docs/R2_SLICE.md section 2): each part gets its own
TripUpdate with its own `stop_sequence`s, the junction call shared by two parts carries the
arrival in the earlier part and the departure in the later, and railway points that are not
passenger stops are never published. A platform mapped to a static boarding point (a track)
becomes `assigned_stop_id`, even on a call without predicted times; a platform that only has a
label (a big station's platform number) is not published here.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as rt

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.model import CallState, FeedState, Instance, JourneyKey, PartSpan, VehicleId
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


def feed_message(state: FeedState, now: Instant) -> rt.FeedMessage:
    message = rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.incrementality = rt.FeedHeader.FULL_DATASET
    message.header.timestamp = _epoch(now)

    vehicles_of: dict[JourneyKey, list[VehicleId]] = defaultdict(list)
    for vehicle, current in sorted(state.vehicles.items()):
        if current.binding is not None:
            vehicles_of[current.binding.journey].append(vehicle)

    for journey, instance in sorted(state.instances.items()):
        if instance.lifecycle not in LIVE or instance.freshness.lost:
            continue  # a stale journey keeps its predictions through a reception gap
        parts = _parts(instance)
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
            entity = message.entity.add()
            entity.id = _entity_id(instance, part)
            update = entity.trip_update
            _trip(update.trip, part.trip_id, journey)
            if vehicles_of[journey]:
                update.vehicle.id = str(vehicles_of[journey][0])
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

    for vehicle, current in sorted(state.vehicles.items()):
        binding = current.binding
        if binding is None or current.position is None:
            continue
        instance = state.instances.get(binding.journey)
        if instance is None or instance.lifecycle not in LIVE or instance.freshness.stale:
            continue
        entity = message.entity.add()
        entity.id = f"vehicle:{vehicle}"
        position = entity.vehicle
        _trip(position.trip, _current_part(instance), instance.journey)
        position.vehicle.id = str(vehicle)
        position.position.latitude = current.position.lat
        position.position.longitude = current.position.lon
        if current.position.bearing is not None:
            position.position.bearing = current.position.bearing
        position.timestamp = _epoch(current.last_seen)
    return message


def write_feed(path: Path, message: rt.FeedMessage) -> None:
    with atomic_output_path(path) as temporary:
        temporary.write_bytes(message.SerializeToString(deterministic=True))
