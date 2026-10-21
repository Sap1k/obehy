"""Core realtime types (docs/R1_SLICE.md section 1): identity, instance state, effects.

Observations and facts are in `facts.py` and re-exported here.

Values are frozen and slotted. `FeedState` containers are updated in place by `core.step`, but
every value stored in them is immutable, so a state can be snapshotted by copying the mappings.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, StrEnum
from typing import Literal, NamedTuple

from obehy.realtime.facts import (
    FACT_SCHEMA_VERSION as FACT_SCHEMA_VERSION,
)
from obehy.realtime.facts import (
    FEEDS as FEEDS,
)
from obehy.realtime.facts import (
    READABLE_FACT_SCHEMAS as READABLE_FACT_SCHEMAS,
)
from obehy.realtime.facts import (
    Delay as Delay,
)
from obehy.realtime.facts import (
    DelayReference as DelayReference,
)
from obehy.realtime.facts import (
    Destination as Destination,
)
from obehy.realtime.facts import (
    EventKind as EventKind,
)
from obehy.realtime.facts import (
    Fact as Fact,
)
from obehy.realtime.facts import (
    Feed as Feed,
)
from obehy.realtime.facts import (
    Interval as Interval,
)
from obehy.realtime.facts import (
    LineRef as LineRef,
)
from obehy.realtime.facts import (
    NextPoint as NextPoint,
)
from obehy.realtime.facts import (
    NextStop as NextStop,
)
from obehy.realtime.facts import (
    NextStopName as NextStopName,
)
from obehy.realtime.facts import (
    NextStopPrediction as NextStopPrediction,
)
from obehy.realtime.facts import (
    Observation as Observation,
)
from obehy.realtime.facts import (
    PlatformAssignment as PlatformAssignment,
)
from obehy.realtime.facts import (
    PlatformLabel as PlatformLabel,
)
from obehy.realtime.facts import (
    PointEvent as PointEvent,
)
from obehy.realtime.facts import (
    Position as Position,
)
from obehy.realtime.facts import (
    RawRef as RawRef,
)
from obehy.realtime.facts import (
    ServiceDay as ServiceDay,
)
from obehy.realtime.facts import (
    SourceDeparture as SourceDeparture,
)
from obehy.realtime.facts import (
    SourceSemantics as SourceSemantics,
)
from obehy.realtime.facts import (
    SourceState as SourceState,
)
from obehy.realtime.facts import (
    StopEvent as StopEvent,
)
from obehy.realtime.facts import (
    TripKey as TripKey,
)
from obehy.realtime.facts import (
    TripStatus as TripStatus,
)
from obehy.realtime.facts import (
    VehicleAttribute as VehicleAttribute,
)
from obehy.realtime.facts import (
    VehicleKey as VehicleKey,
)
from obehy.realtime.times import Instant

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


BindingMethod = Literal["keyed", "inferred"]


@dataclass(frozen=True, slots=True)
class Binding:
    vehicle: VehicleId
    journey: JourneyKey
    trip_id: str
    method: BindingMethod
    bound_at: Instant
    # An inferred binding's stop events go to history only once continuity confirms it
    # (BASE_PLAN.md 19.2); a keyed binding is confirmed by its key.
    confirmed: bool = True


@dataclass(frozen=True, slots=True)
class Departure:
    """A keyless vehicle left the stop named `stop` (its next stop until then: the next-stop
    name changed, ARRIVA-Q2) at `left_at`; `scheduled` is the departure time the source's delay
    implies, if it gave one."""

    stop: str
    left_at: Instant
    scheduled: Interval | None
    position: Position | None = None  # where the vehicle was when the name changed


@dataclass(frozen=True, slots=True)
class KeylessEpisode:
    """What a keyless vehicle has shown since its line and destination last changed
    (`infer/keyless/episode.py`)."""

    line: str
    destination: str
    started_at: Instant
    last_at: Instant
    next_stop: str | None
    departures: tuple[Departure, ...] = ()


# --- instance state ----------------------------------------------------------------------------

Lifecycle = Literal["forecast", "pre_trip", "running", "finished"]
CallStatus = Literal["scheduled", "predicted", "actual", "inferred", "no_realtime", "cancelled"]
SourceClass = Literal["gps", "source", "propagated"]


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

    def estimated(
        self,
        arrival: Instant | None,
        departure: Instant | None,
        status: CallStatus,
        source_class: SourceClass | None,
    ) -> CallState:
        """A copy with new estimates; `dataclasses.replace` is too slow for estimating every
        call of every moving journey each tick."""

        return CallState(
            self.sequence,
            self.location_id,
            self.visit_n,
            self.arrival,
            self.departure,
            arrival,
            departure,
            status,
            source_class,
            self.passed_arrival,
            self.passed_departure,
            self.passenger,
            self.platform,
        )


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
    at_us: int  # `at` in exact microseconds (`times.micros`), for the tracker's arithmetic
    # Since when the fixes have been off the path for this reading (held, as if missing).
    off_path_since: Instant | None = None
    # How far it has been advanced by the distance driven since its last fix on the path.
    reckoned_m: float = 0.0

    def evolve(
        self,
        *,
        log_p: float | None = None,
        history: tuple[Placement, ...] | None = None,
    ) -> Hypothesis:
        """A copy with some fields changed; `dataclasses.replace` is too slow for the tracker's
        inner loop."""

        return Hypothesis(
            self.along_m,
            self.at,
            self.lateness_s,
            self.log_p if log_p is None else log_p,
            self.history if history is None else history,
            self.at_us,
            self.off_path_since,
            self.reckoned_m,
        )


@dataclass(frozen=True, slots=True)
class OffPath:
    """Where the vehicle is estimated to be along a stop-to-stop chord while its fixes are off
    it: placed on the leg between the last stop left and the next one by its distances to the
    two (`progress.py`). Lateness only, never events."""

    along_m: float
    at: Instant
    lateness_s: float


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
    # While the best reading is held off a stop-to-stop chord: the estimate from the latest fix.
    off_path: OffPath | None = None
    # The newest fix's source and place (lon, lat): the distance driven to the next one.
    fix: tuple[str, float, float] | None = None

    @property
    def lateness_s(self) -> float | None:
        """The measured lateness: off a chord the estimate, else the best reading's."""

        if self.off_path is not None:
            return self.off_path.lateness_s
        return self.hypotheses[0].lateness_s if self.hypotheses else None


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


class Keep(Enum):
    """The default of an `evolve` argument: keep the field (None is a value)."""

    KEEP = 0


KEEP = Keep.KEEP


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

    def evolve(
        self,
        *,
        heard_at: Instant | Keep = KEEP,
        fingerprint: int | None | Keep = KEEP,
        next_point: NextPoint | None | Keep = KEEP,
        delay_s: int | None | Keep = KEEP,
        delay_at: Instant | None | Keep = KEEP,
        delay_reference: DelayReference | None | Keep = KEEP,
        prediction: NextStopPrediction | None | Keep = KEEP,
        point: int | None | Keep = KEEP,
        position_at: Instant | None | Keep = KEEP,
        lead: Lead | None | Keep = KEEP,
    ) -> SourceTrack:
        """A copy with some fields replaced, faster than `dataclasses.replace` (per fix)."""

        return SourceTrack(
            self.source,
            self.heard_at if heard_at is KEEP else heard_at,
            self.fingerprint if fingerprint is KEEP else fingerprint,
            self.next_point if next_point is KEEP else next_point,
            self.delay_s if delay_s is KEEP else delay_s,
            self.delay_at if delay_at is KEEP else delay_at,
            self.delay_reference if delay_reference is KEEP else delay_reference,
            self.prediction if prediction is KEEP else prediction,
            self.point if point is KEEP else point,
            self.position_at if position_at is KEEP else position_at,
            self.lead if lead is KEEP else lead,
        )


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

    def evolve(
        self,
        *,
        lifecycle: Lifecycle | Keep = KEEP,
        calls: tuple[CallState, ...] | Keep = KEEP,
        freshness: Freshness | Keep = KEEP,
        progress: Progress | None | Keep = KEEP,
        track: Track | None | Keep = KEEP,
        delay_s: int | None | Keep = KEEP,
        off_route_since: Instant | None | Keep = KEEP,
        off_route: bool | Keep = KEEP,
        lead: Lead | None | Keep = KEEP,
        sources: tuple[SourceTrack, ...] | Keep = KEEP,
        track_source: str | None | Keep = KEEP,
    ) -> Instance:
        """A copy with the fields the timeline changes per fix replaced; `dataclasses.replace`
        is several times slower, and this runs a few times for every observation."""

        return Instance(
            self.journey,
            self.release_id,
            self.trip_id,
            self.mode,
            self.lifecycle if lifecycle is KEEP else lifecycle,
            self.calls if calls is KEEP else calls,
            self.freshness if freshness is KEEP else freshness,
            self.progress if progress is KEEP else progress,
            self.track if track is KEEP else track,
            self.delay_s if delay_s is KEEP else delay_s,
            self.off_route_since if off_route_since is KEEP else off_route_since,
            self.off_route if off_route is KEEP else off_route,
            self.lead if lead is KEEP else lead,
            self.journeys,
            self.parts,
            self.sources if sources is KEEP else sources,
            self.track_source if track_source is KEEP else track_source,
        )

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
    episode: KeylessEpisode | None = None
    # The facts of the last observation: the same fix again (`last_seen` is its own fix time)
    # is no news and is not processed again.
    facts: tuple[Fact, ...] = ()
    # Reception time of the first observation this state depends on: the start of the current
    # binding, or of the keyless episode; otherwise the last observation. Warm replay starts
    # the vehicle there (`ObservationResult.state_since`).
    since: Instant | None = None


# A hop between consecutive passenger calls: (mode, from location, to location, timed from the
# first call's departure rather than its arrival: at the origin, which has no arrival).
HopKey = tuple[str, str, str, bool]
# One vehicle's traversal of a hop: (arrival at its end, seconds taken, seconds scheduled).
Traversal = tuple[Instant, float, float]


@dataclass(slots=True)
class FeedState:
    feed: Feed
    vehicles: dict[VehicleId, VehicleState] = field(default_factory=dict[VehicleId, VehicleState])
    instances: dict[JourneyKey, Instance] = field(default_factory=dict[JourneyKey, Instance])
    # Journeys whose per-call estimates are out of date; `core.estimate_all` refreshes them.
    dirty: set[JourneyKey] = field(default_factory=set[JourneyKey])
    # Recent traversals of each hop by any journey, oldest first (`timeline.runtimes`).
    hops: dict[HopKey, deque[Traversal]] = field(default_factory=dict[HopKey, deque[Traversal]])


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
    # The vehicle's `VehicleState.since` after this observation; None without a vehicle.
    state_since: Instant | None = None


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
