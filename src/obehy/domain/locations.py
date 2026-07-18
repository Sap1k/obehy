from __future__ import annotations

from dataclasses import dataclass

from obehy.domain.identifiers import CanonicalId, EntityKind
from obehy.domain.schedule import ServiceTime


@dataclass(frozen=True, slots=True)
class PassengerCall:
    sequence: int
    stop_place_id: CanonicalId
    boarding_point_id: CanonicalId
    scheduled_arrival: ServiceTime | None
    scheduled_departure: ServiceTime | None
    pickup_allowed: bool
    dropoff_allowed: bool

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("Call sequence must be positive")
        if self.stop_place_id.kind is not EntityKind.STOP_PLACE:
            raise ValueError("Passenger calls reference a stop place")
        if self.boarding_point_id.kind is not EntityKind.BOARDING_POINT:
            raise ValueError("Passenger calls require a boarding point")
        if self.scheduled_arrival is None and self.scheduled_departure is None:
            raise ValueError("Passenger calls require an arrival or departure time")


@dataclass(frozen=True, slots=True)
class OperationalCall:
    sequence: int
    operational_point_id: CanonicalId
    scheduled_passage: ServiceTime

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("Call sequence must be positive")
        if self.operational_point_id.kind is not EntityKind.OPERATIONAL_POINT:
            raise ValueError("Operational calls reference an operational point")
