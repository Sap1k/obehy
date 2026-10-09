"""Per-feed GTFS-RT from core state (BASE_PLAN.md section 30): matched vehicles only.

Output is deterministic: entities in sorted order, timestamps from the tick clock and the
observations, never from the wall clock. Stale and finished journeys are left out.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as rt

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.model import FeedState, Instance, JourneyKey, VehicleId
from obehy.realtime.times import Instant

LIVE = ("pre_trip", "running")


def _epoch(value: Instant) -> int:
    return int(value.timestamp())


def _trip(descriptor: rt.TripDescriptor, instance: Instance) -> None:
    descriptor.trip_id = instance.trip_id
    descriptor.start_date = instance.journey.service_date.strftime("%Y%m%d")
    descriptor.schedule_relationship = rt.TripDescriptor.SCHEDULED


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
        updates = [c for c in instance.calls if c.status in ("actual", "inferred", "predicted")]
        if not updates:
            continue
        entity = message.entity.add()
        entity.id = f"trip:{journey.namespace}:{journey.key}:{journey.service_date.isoformat()}"
        update = entity.trip_update
        _trip(update.trip, instance)
        if vehicles_of[journey]:
            update.vehicle.id = str(vehicles_of[journey][0])
        update.timestamp = _epoch(instance.freshness.updated_at)
        for call in updates:
            stop = update.stop_time_update.add()
            stop.stop_sequence = call.sequence
            if call.estimated_arrival is not None:
                stop.arrival.time = _epoch(call.estimated_arrival)
            if call.estimated_departure is not None:
                stop.departure.time = _epoch(call.estimated_departure)

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
        _trip(position.trip, instance)
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
