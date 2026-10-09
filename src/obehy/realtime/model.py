"""Core realtime types (docs/R1_SLICE.md section 1).

Values are frozen and slotted. `FeedState` containers are updated in place by `core.step`, but
every value stored in them is immutable, so a state can be snapshotted by copying the mappings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal

from obehy.realtime.times import Instant

Feed = Literal["jdf", "czptt"]
FEEDS: tuple[Feed, ...] = ("jdf", "czptt")

FACT_SCHEMA_VERSION = 1


# --- observations -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class RawRef:
    """The archived payload an observation was decoded from, and its position in it."""

    sha256: str
    item: int


@dataclass(frozen=True, slots=True)
class VehicleKey:
    source_vehicle_id: str


@dataclass(frozen=True, slots=True)
class TripKey:
    namespace: str
    key: str


@dataclass(frozen=True, slots=True)
class Position:
    lat: float
    lon: float
    bearing: float | None = None


DelayReference = Literal["arrival", "departure", "unknown"]


@dataclass(frozen=True, slots=True)
class Delay:
    seconds: int
    reference: DelayReference


@dataclass(frozen=True, slots=True)
class SourceState:
    """A source's own trip state code, interpreted by policy per source."""

    code: str


EventKind = Literal["arrival", "departure", "passage"]


@dataclass(frozen=True, slots=True)
class StopEvent:
    call_ref: str
    kind: EventKind
    at: Instant


@dataclass(frozen=True, slots=True)
class NextStop:
    call_ref: str


Fact = VehicleKey | TripKey | Position | Delay | SourceState | StopEvent | NextStop


@dataclass(frozen=True, slots=True)
class Observation:
    source: str
    channel: str
    feed: Feed
    received_at: Instant
    observed_at: Instant | None
    raw: RawRef
    decoder_version: int
    facts: tuple[Fact, ...]

    def first[T](self, kind: type[T]) -> T | None:
        for fact in self.facts:
            if isinstance(fact, kind):
                return fact
        return None

    @property
    def at(self) -> Instant:
        """The best time of the observation: the source time, else reception."""

        return self.observed_at if self.observed_at is not None else self.received_at


# --- identity ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class VehicleId:
    source: str
    source_vehicle_id: str

    def __str__(self) -> str:
        return f"{self.source}:{self.source_vehicle_id}"


@dataclass(frozen=True, slots=True, order=True)
class JourneyKey:
    """The public and history identity of an operating instance (BASE_PLAN.md section 29)."""

    feed: Feed
    namespace: str
    key: str
    service_date: date


class Reason(StrEnum):
    """Why an observation is not (or no longer) bound to a journey."""

    NO_KEY = "no_key"
    NO_LINE = "no_line"
    NO_TRIP = "no_trip"
    NOT_ACTIVE = "not_active"
    NOT_IN_SERVICE = "not_in_service"
    AMBIGUOUS = "ambiguous"
    UNTIMED = "untimed"


BindingMethod = Literal["keyed"]


@dataclass(frozen=True, slots=True)
class Binding:
    vehicle: VehicleId
    journey: JourneyKey
    trip_id: str
    method: BindingMethod
    bound_at: Instant


# --- instance state ----------------------------------------------------------------------------

Lifecycle = Literal["forecast", "pre_trip", "running", "finished"]
CallStatus = Literal["scheduled", "predicted", "actual", "no_realtime", "cancelled"]
SourceClass = Literal["gps", "source", "propagated"]


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    lo: Instant
    hi: Instant


@dataclass(frozen=True, slots=True)
class CallState:
    sequence: int
    location_id: str
    visit_n: int
    arrival: Interval | None = None
    departure: Interval | None = None
    estimated_arrival: Instant | None = None
    estimated_departure: Instant | None = None
    status: CallStatus = "scheduled"
    source_class: SourceClass | None = None


@dataclass(frozen=True, slots=True)
class Progress:
    """Distance along the journey's path and the last call reached or passed."""

    distance_m: float
    call_index: int
    at: Instant


@dataclass(frozen=True, slots=True)
class Instance:
    journey: JourneyKey
    release_id: str
    trip_id: str
    lifecycle: Lifecycle
    calls: tuple[CallState, ...]
    updated_at: Instant
    progress: Progress | None = None
    delay_s: int | None = None
    off_route_since: Instant | None = None
    off_route: bool = False
    stale: bool = False


VehicleStatus = Literal["running", "positioning", "layover", "unmatched", "not_in_service"]


@dataclass(frozen=True, slots=True)
class VehicleState:
    vehicle: VehicleId
    feed: Feed
    status: VehicleStatus
    last_seen: Instant
    trip_key: TripKey | None = None
    binding: Binding | None = None
    position: Position | None = None
    reason: Reason | None = None


@dataclass(slots=True)
class FeedState:
    feed: Feed
    vehicles: dict[VehicleId, VehicleState] = field(default_factory=dict[VehicleId, VehicleState])
    instances: dict[JourneyKey, Instance] = field(default_factory=dict[JourneyKey, Instance])
    # Journeys whose per-call estimates are out of date; `core.estimate_all` refreshes them.
    dirty: set[JourneyKey] = field(default_factory=set[JourneyKey])


# --- effects ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduledCall:
    ordinal: int
    location_id: str
    visit_n: int
    passenger_service: bool
    scheduled_arrival: int | None
    scheduled_departure: int | None
    name: str


@dataclass(frozen=True, slots=True)
class SnapshotJourney:
    journey: JourneyKey
    at: Instant
    release_id: str
    trip_id: str
    route_name: str
    headsign: str | None
    calls: tuple[ScheduledCall, ...]


@dataclass(frozen=True, slots=True)
class WriteEvent:
    journey: JourneyKey
    location_id: str
    visit_n: int
    kind: EventKind
    interval: Interval
    method: Literal["source", "progress"]
    source: str


@dataclass(frozen=True, slots=True)
class AssignVehicle:
    vehicle: VehicleId
    journey: JourneyKey
    at: Instant


@dataclass(frozen=True, slots=True)
class ObservationResult:
    observation: Observation
    journey: JourneyKey | None
    reason: Reason | None


Effect = SnapshotJourney | WriteEvent | AssignVehicle | ObservationResult


@dataclass(frozen=True, slots=True)
class Derivation:
    core_version: str
    policy_version: str
    release_id: str


def freeze[K, V](mapping: Mapping[K, V]) -> dict[K, V]:
    """A shallow copy of a state mapping (values are immutable)."""

    return dict(mapping)
