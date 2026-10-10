"""Observations and the facts sources state (docs/R1_SLICE.md and R2_SLICE.md section 3).

Frozen and slotted; a fact's JSON form for `rt.observation` is in `model_json.py`, versioned by
`FACT_SCHEMA_VERSION`. `model.py` re-exports everything here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from obehy.realtime.times import Instant

Feed = Literal["jdf", "czptt"]
FEEDS: tuple[Feed, ...] = ("jdf", "czptt")

FACT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    lo: Instant
    hi: Instant


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


# `point`: measured at the last railway point the source reports (SŽ `de`).
DelayReference = Literal["arrival", "departure", "unknown", "point"]


@dataclass(frozen=True, slots=True)
class Delay:
    seconds: int
    reference: DelayReference


@dataclass(frozen=True, slots=True)
class SourceState:
    """A source's own trip state code, interpreted by policy per source."""

    code: str


@dataclass(frozen=True, slots=True)
class SourceSemantics:
    """How the core reads one channel's facts where sources differ, from its manifest's
    `[channel.semantics]`. The defaults take every fact at face value."""

    # In a pre-trip state the delay is the time since the scheduled departure (DUK-Q14).
    pre_trip_delay_is_elapsed: bool = False
    # A pre-trip key after the trip's scheduled end is left over from an earlier trip (DUK-Q15).
    pre_trip_after_end_is_stale: bool = False
    # A vehicle's entry repeated unchanged is no news: no fix, no event, no freshness (SZ-Q6).
    unchanged_entry_is_no_news: bool = False


@dataclass(frozen=True, slots=True)
class SourceDeparture:
    """The departure the source plans for the run of the trip it reports (DÚK `TODepartureDT`)."""

    at: Instant


EventKind = Literal["arrival", "departure", "passage"]


@dataclass(frozen=True, slots=True)
class StopEvent:
    call_ref: str
    kind: EventKind
    at: Instant


@dataclass(frozen=True, slots=True)
class NextStop:
    call_ref: str


@dataclass(frozen=True, slots=True)
class ServiceDay:
    """The operating date the source names for the run it reports (SŽ `id`): the only
    candidate date for binding."""

    day: date


@dataclass(frozen=True, slots=True)
class PointEvent:
    """The last railway point the source reports the train reached or passed (SŽ `cna`): its
    name, the SR70 codes (5 digits) the catalogue gives that name, the timetabled time and when
    it happened (a minute: `[T, T + 59 s]`); `standing` while the train stands there. Several
    codes share some names (SZ-Q4); the run's calls decide which is meant."""

    name: str
    codes: tuple[str, ...]
    scheduled: Instant | None
    actual: Interval
    standing: bool


@dataclass(frozen=True, slots=True)
class NextPoint:
    """The next railway point (SŽ `nna`, `zst_sr70` without its check digit)."""

    name: str
    sr70: str | None


@dataclass(frozen=True, slots=True)
class NextStopPrediction:
    """The source's own prediction for the next passenger stop (SŽ `nsn70`, `nst`, `nsp`)."""

    sr70: str
    scheduled: Instant | None
    predicted: Instant | None


@dataclass(frozen=True, slots=True)
class TripStatus:
    replacement_bus: bool
    diverted: bool


PlatformLabel = Literal["platform", "track"]


@dataclass(frozen=True, slots=True)
class PlatformAssignment:
    """A station board row (SZT): where the train with this key arrives or departs at the
    station (SR70, 5 digits) at its scheduled time. `label` says what the value numbers."""

    station: str
    kind: Literal["arrival", "departure"]
    scheduled: Instant
    value: str
    label: PlatformLabel


Fact = (
    VehicleKey
    | TripKey
    | Position
    | Delay
    | SourceState
    | SourceDeparture
    | StopEvent
    | NextStop
    | ServiceDay
    | PointEvent
    | NextPoint
    | NextStopPrediction
    | TripStatus
    | PlatformAssignment
)


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

    def all[T](self, kind: type[T]) -> tuple[T, ...]:
        return tuple(fact for fact in self.facts if isinstance(fact, kind))

    @property
    def at(self) -> Instant:
        """The best time of the observation: the source time, else reception."""

        return self.observed_at if self.observed_at is not None else self.received_at
