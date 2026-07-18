from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import IntEnum, StrEnum

from obehy.domain.identifiers import CanonicalId, CisLineId, CisTripId, TrainNumber


class TransitMode(StrEnum):
    BUS = "bus"
    TRAM = "tram"
    TROLLEYBUS = "trolleybus"
    METRO = "metro"
    FERRY = "ferry"
    CABLE_CAR = "cable_car"
    RAIL = "rail"


class Direction(IntEnum):
    OUTBOUND = 0
    INBOUND = 1


@dataclass(frozen=True, slots=True, order=True)
class ServiceTime:
    seconds: int

    def __post_init__(self) -> None:
        if isinstance(self.seconds, bool) or self.seconds < 0:
            raise ValueError("Service time must be non-negative")


@dataclass(frozen=True, slots=True)
class ServiceCalendar:
    valid_from: date
    valid_to: date
    weekdays: frozenset[int]
    added_dates: frozenset[date] = frozenset()
    removed_dates: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if self.valid_to < self.valid_from:
            raise ValueError("Calendar validity cannot end before it starts")
        if not self.weekdays <= set(range(7)):
            raise ValueError("Weekdays use Python numbering 0=Monday through 6=Sunday")
        if self.added_dates & self.removed_dates:
            raise ValueError("A service date cannot be both added and removed")

    def operates_on(self, service_date: date) -> bool:
        if service_date in self.added_dates:
            return True
        if service_date in self.removed_dates:
            return False
        return (
            self.valid_from <= service_date <= self.valid_to
            and service_date.weekday() in self.weekdays
        )


@dataclass(frozen=True, slots=True)
class TripInstance:
    canonical_trip_id: CanonicalId
    operating_date: date

    def __post_init__(self) -> None:
        if self.canonical_trip_id.kind.value != "scheduled_trip":
            raise ValueError("TripInstance requires a scheduled-trip canonical ID")


@dataclass(frozen=True, slots=True)
class RoadTripCandidate:
    canonical_trip_id: CanonicalId
    cis_line_id: CisLineId
    cis_trip_id: CisTripId
    calendar: ServiceCalendar


class TripResolutionError(LookupError):
    pass


class TripNotFoundError(TripResolutionError):
    pass


class AmbiguousTripError(TripResolutionError):
    pass


def resolve_road_trip(
    candidates: Iterable[RoadTripCandidate],
    cis_line_id: CisLineId,
    cis_trip_id: CisTripId,
    service_date: date,
) -> TripInstance:
    matches = [
        candidate
        for candidate in candidates
        if candidate.cis_line_id == cis_line_id
        and candidate.cis_trip_id == cis_trip_id
        and candidate.calendar.operates_on(service_date)
    ]
    if not matches:
        raise TripNotFoundError("No active road timetable variant matches the requested identity")
    if len(matches) != 1:
        raise AmbiguousTripError(
            "Multiple active road timetable variants match the requested identity"
        )
    return TripInstance(matches[0].canonical_trip_id, service_date)


@dataclass(frozen=True, slots=True)
class RailTripCandidate:
    canonical_trip_id: CanonicalId
    train_number: TrainNumber
    direction: Direction
    calendar: ServiceCalendar
    passenger_stop_place_ids: tuple[CanonicalId, ...]
    passenger_call_sequences: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.passenger_stop_place_ids) != len(self.passenger_call_sequences):
            raise ValueError("Rail passenger stops and call sequences must have equal lengths")
        if tuple(sorted(self.passenger_call_sequences)) != self.passenger_call_sequences:
            raise ValueError("Rail passenger call sequences must be ordered")


@dataclass(frozen=True, slots=True)
class TrainSegmentMatch:
    trip_instance: TripInstance
    matched_call_sequences: tuple[int, ...]


def _subsequence_positions(
    complete: tuple[CanonicalId, ...], segment: tuple[CanonicalId, ...]
) -> tuple[int, ...] | None:
    positions: list[int] = []
    cursor = 0
    for wanted in segment:
        try:
            found = complete.index(wanted, cursor)
        except ValueError:
            return None
        positions.append(found)
        cursor = found + 1
    return tuple(positions)


def resolve_train_segment(
    candidates: Iterable[RailTripCandidate],
    train_number: TrainNumber,
    service_date: date,
    direction: Direction,
    segment_stop_place_ids: tuple[CanonicalId, ...],
) -> TrainSegmentMatch:
    if not segment_stop_place_ids:
        raise ValueError("A regional train segment must contain at least one passenger call")
    matches: list[tuple[RailTripCandidate, tuple[int, ...]]] = []
    for candidate in candidates:
        if (
            candidate.train_number != train_number
            or candidate.direction != direction
            or not candidate.calendar.operates_on(service_date)
        ):
            continue
        positions = _subsequence_positions(
            candidate.passenger_stop_place_ids, segment_stop_place_ids
        )
        if positions is not None:
            matches.append((candidate, positions))
    if not matches:
        raise TripNotFoundError("No full train contains the regional passenger-call subsequence")
    if len(matches) != 1:
        raise AmbiguousTripError(
            "Multiple full trains contain the regional passenger-call subsequence"
        )
    candidate, positions = matches[0]
    return TrainSegmentMatch(
        TripInstance(candidate.canonical_trip_id, service_date),
        tuple(candidate.passenger_call_sequences[position] for position in positions),
    )
