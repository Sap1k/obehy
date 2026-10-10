"""Core realtime types (docs/R1_SLICE.md section 1).

Values are frozen and slotted. `FeedState` containers are updated in place by `core.step`, but
every value stored in them is immutable, so a state can be snapshotted by copying the mappings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal, NamedTuple

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
    STALE_KEY = "stale_key"


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
CallStatus = Literal["scheduled", "predicted", "actual", "inferred", "no_realtime", "cancelled"]
SourceClass = Literal["gps", "source", "propagated"]


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    lo: Instant
    hi: Instant


@dataclass(frozen=True, slots=True)
class Platform:
    """The platform or track a source assigned to a call (docs/R2_SLICE.md section 6): the raw
    value is what is shown; `boarding_point_id` only where a track maps to a static one."""

    value: str
    label: PlatformLabel
    boarding_point_id: str | None
    assigned_at: Instant
    source: str


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
    # When progress crossed the call's triggers, interpolated between the fixes either side.
    # Set for every committed crossing, also one inside a reception gap that `arrival` and
    # `departure` (recorded events, the only ones history gets) leave out: realtime only.
    passed_arrival: Instant | None = None
    passed_departure: Instant | None = None
    # Railway points that are not passenger stops are tracked but never published.
    passenger: bool = True
    platform: Platform | None = None


@dataclass(frozen=True, slots=True)
class Progress:
    """Distance along the journey's path and the last call reached or passed."""

    distance_m: float
    call_index: int
    at: Instant


class Placement(NamedTuple):
    """Where a reading placed one fix along the path."""

    along_m: float
    at: Instant


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """One reading of where the vehicle is along the path (BASE_PLAN.md section 20.4)."""

    along_m: float
    at: Instant
    lateness_s: float
    log_p: float
    # Each fix since the commit point, oldest first, ending at the last fix this reading placed
    # on the path.
    history: tuple[Placement, ...]
    # Since when the fixes have been off the path for this reading (held, as if missing).
    off_path_since: Instant | None = None

    def evolve(
        self,
        *,
        log_p: float | None = None,
        history: tuple[Placement, ...] | None = None,
        off_path_since: Instant | None = None,
    ) -> Hypothesis:
        """A copy with some fields changed; `dataclasses.replace` is too slow for the tracker's
        inner loop. `off_path_since` can be set, never cleared."""

        return Hypothesis(
            self.along_m,
            self.at,
            self.lateness_s,
            self.log_p if log_p is None else log_p,
            self.history if history is None else history,
            self.off_path_since if off_path_since is None else off_path_since,
        )


@dataclass(frozen=True, slots=True)
class Track:
    """The live hypotheses of a journey and how far events are committed."""

    hypotheses: tuple[Hypothesis, ...]
    committed_m: float
    unmatched_since: Instant | None = None
    # The newest fix time used; a fix no newer (repeated or out of order) is skipped.
    seen_at: Instant | None = None
    # Time of the last committed crossing: crossings are committed in path order, so their
    # times never decrease.
    crossed_at: Instant | None = None


@dataclass(frozen=True, slots=True)
class Freshness:
    """How current a journey's data is."""

    updated_at: Instant  # source time of the last observation
    # When the last observation bringing anything new was received (reception clock, so a
    # vehicle clock offset or a frozen GPS time does not matter); staleness counts from here.
    heard_at: Instant
    # No new observation for `stale_after_s`: the position is withdrawn, predictions continue.
    stale: bool = False
    # No new observation for `predict_without_data_s`: predictions are withdrawn too.
    lost: bool = False


@dataclass(frozen=True, slots=True)
class Lead:
    """The one vehicle whose observations drive the timeline when several claim the journey
    (DUK-Q11); it changes only when the lead goes stale."""

    vehicle: VehicleId
    seen: Instant


@dataclass(frozen=True, slots=True)
class JourneySpan:
    """A public journey of a run instance: the run's calls `first..last` (rail: one per train
    number; consecutive journeys share the call where the number changes)."""

    journey: JourneyKey
    first: int
    last: int


@dataclass(frozen=True, slots=True)
class PartSpan:
    """A published trip part of a run instance: the run's calls `first..last`."""

    trip_id: str
    first: int
    last: int


@dataclass(frozen=True, slots=True)
class SourceTrack:
    """What one source has said about a journey (fusion, docs/R2_SLICE.md section 5)."""

    source: str
    heard_at: Instant  # reception time of the last observation bringing anything new
    fingerprint: int | None = None  # of the last observation's facts (SZ-Q6)
    next_point: NextPoint | None = None
    delay_s: int | None = None
    delay_at: Instant | None = None
    delay_reference: DelayReference | None = None  # `point` once it gave a point delay
    prediction: NextStopPrediction | None = None
    point: int | None = None  # index of the last call the source placed the train at
    position_at: Instant | None = None  # when it last gave a usable new position
    # The vehicle leading this source's contributions (DUK-Q11 per source); the instance's own
    # `lead` is the first source's.
    lead: Lead | None = None


@dataclass(frozen=True, slots=True)
class Instance:
    journey: JourneyKey
    release_id: str
    trip_id: str
    mode: str  # the route mode, for per-mode policy values
    lifecycle: Lifecycle
    calls: tuple[CallState, ...]
    freshness: Freshness
    progress: Progress | None = None
    track: Track | None = None
    delay_s: int | None = None
    off_route_since: Instant | None = None
    off_route: bool = False
    lead: Lead | None = None
    # Rail runs: the public journeys and published trip parts over the run's calls; empty for
    # a road trip, which is its own journey and part.
    journeys: tuple[JourneySpan, ...] = ()
    parts: tuple[PartSpan, ...] = ()
    sources: tuple[SourceTrack, ...] = ()
    track_source: str | None = None  # the source of the tracker's last fix

    def source_track(self, source: str) -> SourceTrack | None:
        for track in self.sources:
            if track.source == source:
                return track
        return None

    def public_call(self, index: int, kind: EventKind) -> tuple[JourneyKey, int]:
        """The public journey and visit number of call `index` for an event of `kind`: at a
        call shared by two journeys the arrival is the earlier one's, the rest the later's."""

        call = self.calls[index]
        spans = [span for span in self.journeys if span.first <= index <= span.last]
        if not spans:
            return self.journey, call.visit_n
        span = spans[0] if kind == "arrival" else spans[-1]
        visit = sum(
            1 for c in self.calls[span.first : index + 1] if c.location_id == call.location_id
        )
        return span.journey, visit

    @property
    def public(self) -> tuple[JourneyKey, ...]:
        return tuple(span.journey for span in self.journeys) or (self.journey,)


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
    run_key: str | None = None  # rail: the CZPTT path (PA) the journey runs on


JourneyLinkKind = Literal["continues_as", "splits_from", "joins"]


@dataclass(frozen=True, slots=True)
class LinkJourneys:
    """Two public journeys of one run: the train continues under a new number."""

    source: JourneyKey
    target: JourneyKey
    kind: JourneyLinkKind


@dataclass(frozen=True, slots=True)
class RecordPlatform:
    """A platform assignment for history (`history.platform_evidence`)."""

    journey: JourneyKey
    location_id: str
    visit_n: int
    kind: Literal["arrival", "departure"]
    platform: Platform


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
    journey: JourneyKey | None  # the instance (rail: the run, `czptt:pa`)
    reason: Reason | None
    # The public journeys the observation bears on (rail: the run's train-number journeys).
    public: tuple[JourneyKey, ...] = ()


@dataclass(frozen=True, slots=True)
class Unresolved:
    """Diagnostics: a source named something the run could not place (an SŽ point name, a
    board row's call); counted by replay, never stored."""

    observation: Observation
    # point: names no call of the run ahead; inconsistent: contradicts the run's other events
    kind: Literal["point", "inconsistent", "platform"]
    name: str


Effect = (
    SnapshotJourney
    | WriteEvent
    | AssignVehicle
    | ObservationResult
    | LinkJourneys
    | RecordPlatform
    | Unresolved
)


@dataclass(frozen=True, slots=True)
class Derivation:
    core_version: str
    policy_version: str
    release_id: str


def freeze[K, V](mapping: Mapping[K, V]) -> dict[K, V]:
    """A shallow copy of a state mapping (values are immutable)."""

    return dict(mapping)
