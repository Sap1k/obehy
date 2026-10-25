"""Which vehicles stand for their journey (BASE_PLAN.md section 30): one marker per journey on
the map, as GTFS-RT publishes one vehicle per journey.

A bus reported by several sources (DÚK and a city's own feed) stands once, as the vehicles of
the source shown (`shown_source`: kept while its fixes are fresh, so the vehicle standing does
not change with every source's fix). A rail run is one train however many vehicles report it:
coupled DÚK units each have a GPS, and the SŽ map is a vehicle of its own; the shown source's
leading vehicle stands for it, else any bound one. The vehicle standing is drawn where the
tracker reads the journey from every source's fixes (`Instance.published`, `shown`). A vehicle
bound to no journey, or to one without an instance, stands for itself.
"""

from __future__ import annotations

from collections import defaultdict

from obehy.realtime.model import (
    FeedState,
    Instance,
    JourneyKey,
    Lead,
    Published,
    VehicleId,
    VehicleState,
)


def _shown_source(instance: Instance) -> str | None:
    return instance.shown_source or instance.track_source


def _lead(instance: Instance, source: str) -> Lead | None:
    lead = instance.lead
    if lead is None or lead.vehicle.source != source:
        track = instance.source_track(source)
        lead = track.lead if track is not None else None
    return lead


def shown(instance: Instance | None, vehicle: VehicleId) -> Published | None:
    """Where `vehicle`, standing for its journey, is drawn: the journey's published reading
    when it is the shown source's lead (or that source's only say) and the journey is not
    stale; None: at its own fix."""

    if instance is None or instance.published is None or instance.freshness.stale:
        return None
    if vehicle.source != _shown_source(instance):
        return None
    lead = _lead(instance, vehicle.source)
    return instance.published if lead is None or lead.vehicle == vehicle else None


def run_position(instance: Instance, bound: list[VehicleState]) -> VehicleState | None:
    """The vehicle that stands for the run: the lead of the shown source, else that source's
    first vehicle, else the first with a position."""

    placed = [v for v in bound if v.position is not None]
    source = _shown_source(instance)
    if source is not None:
        lead = _lead(instance, source)
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
    one bus twice on the map. The shown source is chosen (`shown_source`), else the first source
    with a position; several vehicles of one source stay (DUK-Q11)."""

    sources = {v.source for v in vehicles}
    if len(sources) <= 1:
        return vehicles
    placed = sorted({v.source for v in vehicles if state.vehicles[v].position is not None})
    chosen = _shown_source(instance)
    if chosen in placed:
        source = chosen
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
