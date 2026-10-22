"""Which vehicles stand for their journey (BASE_PLAN.md section 30): one marker per journey on
the map, as GTFS-RT publishes one vehicle per journey.

A bus reported by several sources (DÚK and a city's own feed) stands once, as the vehicles of
the source the tracker follows. A rail run is one train however many vehicles report it:
coupled DÚK units each have a GPS, and the SŽ map is a vehicle of its own; the run's position
is the leading vehicle's of the source fusion last tracked (`track_source`), else any bound
one's. A vehicle bound to no journey, or to one without an instance, stands for itself.
"""

from __future__ import annotations

from collections import defaultdict

from obehy.realtime.model import FeedState, Instance, JourneyKey, VehicleId, VehicleState


def run_position(instance: Instance, bound: list[VehicleState]) -> VehicleState | None:
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


def one_source(instance: Instance, vehicles: list[VehicleId], state: FeedState) -> list[VehicleId]:
    """The vehicles that stand for a journey (not a rail run): those of one source. Several
    sources report some buses (DÚK and a city's own feed); each would be a vehicle of its own,
    one bus twice on the map. The source the tracker follows is chosen (the preferred fresh
    one), else the first source with a position; several vehicles of one source stay
    (DUK-Q11)."""

    sources = {v.source for v in vehicles}
    if len(sources) <= 1:
        return vehicles
    placed = sorted({v.source for v in vehicles if state.vehicles[v].position is not None})
    if instance.track_source in placed:
        source = instance.track_source
    elif placed:
        source = placed[0]
    else:
        source = min(sources)
    return [v for v in vehicles if v.source == source]


def vehicles_by_journey(state: FeedState) -> dict[JourneyKey, list[VehicleId]]:
    """The bound vehicles of each journey, in vehicle order."""

    out: dict[JourneyKey, list[VehicleId]] = defaultdict(list)
    for vehicle, current in sorted(state.vehicles.items()):
        if current.binding is not None:
            out[current.binding.journey].append(vehicle)
    return out


def representing(state: FeedState) -> set[VehicleId]:
    """The feed's vehicles that stand for their journey, and those bound to none."""

    chosen = {v for v, current in state.vehicles.items() if current.binding is None}
    for journey, vehicles in vehicles_by_journey(state).items():
        instance = state.instances.get(journey)
        if instance is None:
            chosen.update(vehicles)
        elif instance.parts:
            lead = run_position(instance, [state.vehicles[v] for v in vehicles])
            chosen.update([lead.vehicle] if lead is not None else vehicles)
        else:
            chosen.update(one_source(instance, vehicles, state))
    return chosen
