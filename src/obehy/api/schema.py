"""Public response models: the OpenAPI contract the frontend's types are generated from.

Trips are identified by their feed `trip_id` and service date; journey keys are attributes.
Times are ISO 8601 in Europe/Prague. Static codes (JDF fixed codes, CZPTT KADR codes, GTFS
boarding codes) are given in readable form (`api/codes.py`), never as raw symbols.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

from obehy.realtime.times import PRAGUE

CallStatus = Literal["scheduled", "predicted", "actual", "inferred", "no_realtime", "cancelled"]
# Every time is answered in Prague time, whether read as a timestamp or from a stored instant.
Time = Annotated[datetime, AfterValidator(lambda value: value.astimezone(PRAGUE))]
SourceClass = Literal["gps", "source", "propagated"]
VehicleState = Literal["running", "positioning", "layover", "unmatched", "not_in_service"]
Boarding = Literal["regular", "none", "phone", "ask_driver"]
WheelchairAmenity = Literal["booking_required", "booking_recommended", "accessible", "partly"]
BicycleAmenity = Literal["prohibited", "reservation_required", "reservation_available", "allowed"]
ReservationAmenity = Literal["required", "available"]
SourceSystem = Literal["jdf", "czptt_note", "czptt_activity"]


class Envelope(BaseModel):
    release_id: str = Field(description="The active release the answer was read from.")
    generated_at: Time


class Problem(BaseModel):
    """An error (RFC 9457 problem details)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None


class Label(BaseModel):
    cs: str
    en: str


class IntegratedSystem(BaseModel):
    """An integrated transport system or city transport network (CIS JŘ code lists)."""

    code: str
    name: str | None
    abbreviation: str | None


class Note(BaseModel):
    kind: str
    code: str | None = Field(default=None, description="A CZPTT central note's code")
    label: str | None
    text: str | None = Field(description="For a CZPTT central note, its official KJŘ text")


class SourceCode(BaseModel):
    """The source code a feature comes from and its official text."""

    system: SourceSystem = Field(
        description="JDF fixed code (pevný kód), CZPTT central note or CZPTT train activity"
    )
    code: str
    name: str | None = Field(description="The short name the timetable prints, where it has one")
    text: str | None = Field(description="The official text of the code")


class Feature(BaseModel):
    """One source code as the timetable states it (a JDF fixed code, a CZPTT central note or
    train activity), with its official Czech text and the typed meanings it carries."""

    kinds: list[str] = Field(description="The typed meanings (assignment kinds) of the code")
    group: str = Field(
        description="accessibility, bicycles, reservation, onboard, operation, stopping, "
        "facility, interchange, calendar, note or integrated_transport"
    )
    icon: str
    label: Label = Field(description="Czech: the code's official text where known")
    official: bool = Field(description="The Czech label is the code's official text")
    scope: str = Field(description="trip, route, service, call, call_range or location")
    from_sequence: int | None = Field(description="First call it applies to; null: all")
    to_sequence: int | None = Field(description="Last call of a call range")
    system: IntegratedSystem | None = None
    note: Note | None = None
    source_code: SourceCode | None = None


class Amenities(BaseModel):
    """What holds for the whole trip; features of some calls only stay in `features`."""

    wheelchair: WheelchairAmenity | None
    bicycles: BicycleAmenity | None
    reservation: ReservationAmenity | None
    refreshments: bool
    luggage: bool
    self_service_ticketing: bool
    on_request: bool = Field(description="Runs only on request (demand responsive)")
    conditional: bool
    integrated_transport: list[IntegratedSystem]
    operates: list[Label] = Field(description="The calendar designations, as printed")


class AlertPeriod(BaseModel):
    start: Time | None
    end: Time | None = Field(description="Null: until further notice")


class AlertTarget(BaseModel):
    """What an alert informs, in the active release."""

    kind: Literal["route", "stop", "trip"]
    id: str = Field(description="route_id, location_id (a stop or boarding point) or trip_id")
    feed: Literal["jdf", "czptt"]
    service_date: date | None = Field(default=None, description="A trip's")
    stop_place: str | None = Field(default=None, description="A stop's stop place")


class Alert(BaseModel):
    """A disruption or notice from a source, at the scope the source gave (BASE_PLAN.md
    section 25). Texts are per language (`cs`, `en`, …); cause, effect and severity are
    GTFS-RT's."""

    alert_id: str = Field(description="`<source>:<the source's ID>`")
    source: str
    source_label: Label | None
    cause: str
    effect: str
    severity: str
    periods: list[AlertPeriod] = Field(description="When it applies; empty: always")
    header: dict[str, str]
    description: dict[str, str]
    url: dict[str, str]
    cause_detail: dict[str, str]
    effect_detail: dict[str, str]
    details: dict[str, str] = Field(description="Source-specific (PID infotexts: display_type)")
    informs: list[AlertTarget] = Field(description="Empty: nothing it names is in the release")
    first_seen: Time
    changed_at: Time
    withdrawn_at: Time | None


class Alerts(Envelope):
    at: Time
    alerts: list[Alert]


class Stop(BaseModel):
    location_id: str
    name: str
    kind: str
    domain: str
    municipality: str | None
    district: str | None
    latitude: float | None
    longitude: float | None
    coordinate_precision: str
    distance_m: float | None = Field(default=None, description="From the `near` point")


class Stops(Envelope):
    stops: list[Stop]


class BoardingPoint(BaseModel):
    location_id: str
    name: str
    public_code: str | None
    latitude: float | None
    longitude: float | None


class Line(BaseModel):
    route_id: str
    short_name: str | None
    long_name: str | None
    mode: str
    color: str | None
    text_color: str | None


class StopPlace(Envelope):
    stop: Stop
    wheelchair_boarding: bool | None
    facilities: list[Feature]
    boarding_points: list[BoardingPoint]
    lines: list[Line] | None = Field(
        default=None, description="Every line calling here, with `include=lines`"
    )


class VehicleTraits(BaseModel):
    """The vehicle's main attributes, effective values (curated > live > model default)."""

    low_floor: bool | None = None
    air_conditioning: bool | None = None
    wheelchair_accessible: bool | None = None
    manufacturer: str | None = None
    model: str | None = Field(default=None, description="Without the manufacturer")
    operator: str | None = None
    licence_plate: str | None = None
    fleet_number: str | None = None


class TripVehicle(BaseModel):
    """The vehicle currently on a trip."""

    vehicle_id: str
    state: VehicleState
    last_seen: Time
    position_at: Time | None = Field(
        default=None,
        description="The source time of the position: a client keeps a newer one it already has",
    )
    latitude: float | None
    longitude: float | None
    bearing: float | None
    traits: VehicleTraits = Field(default_factory=VehicleTraits)
    sources: list[str] = Field(default_factory=list, description="Sources reporting it now")


class Departure(BaseModel):
    """A board row: a departure (or, on an arrivals board, an arrival) at a stop place."""

    trip_id: str
    service_date: date
    location_id: str = Field(description="The stop place of this row (multi-stop boards)")
    sequence: int = Field(
        description="The call's sequence in the trip: a trip calling at the stop place twice "
        "has a row for each call"
    )
    line: Line
    trip_short_name: str | None
    headsign: str | None
    scheduled: Time
    estimated: Time | None
    delay_s: int | None
    status: CallStatus
    source_class: SourceClass | None
    stale: bool | None
    platform: str | None = Field(description="The scheduled platform, track or post code.")
    vehicle: TripVehicle | None = None
    wheelchair: WheelchairAmenity | None = Field(
        default=None,
        description="What the timetable states for the whole trip; the vehicle's traits, when "
        "known, are realtime's",
    )
    sources: list[str] = Field(default_factory=list, description="Where its realtime came from")
    alert_ids: list[str] = Field(
        default_factory=list, description="The board's `alerts` informing its line, stop or trip"
    )


class Departures(Envelope):
    stop: Stop
    stops: list[Stop] = Field(description="Every stop place of a multi-stop board")
    since: Time
    until: Time
    departures: list[Departure]
    next: str | None = Field(description="Cursor for the following rows (`cursor=`)")
    alerts: list[Alert] = Field(description="Current alerts of the stops and the rows' lines")


class Arrivals(Envelope):
    stop: Stop
    stops: list[Stop]
    since: Time
    until: Time
    arrivals: list[Departure]
    next: str | None
    alerts: list[Alert]


class JourneyKey(BaseModel):
    namespace: str
    key: str


class TripCall(BaseModel):
    sequence: int
    location_id: str
    name: str
    latitude: float | None
    longitude: float | None
    boarding: Boarding
    alighting: Boarding
    flags: list[str] = Field(
        description="request_stop, on_request, conditional, exit_only, boarding_only, "
        "not_stopping, border_control_only"
    )
    platform: str | None
    scheduled_arrival: Time | None
    scheduled_departure: Time | None
    estimated_arrival: Time | None
    estimated_departure: Time | None
    status: CallStatus
    source_class: SourceClass | None


class RealtimeSource(BaseModel):
    """A source the trip's realtime came from."""

    source: str
    label: Label | None
    heard_at: Time | None = Field(description="When it last brought anything new")
    position_at: Time | None = Field(description="When it last gave a usable position")
    delay_s: int | None
    leads_position: bool = Field(description="Its positions drive the trip's progress")


class TripRealtime(BaseModel):
    lifecycle: str = Field(description="pre_trip, running, finished or cancelled")
    delay_s: int | None
    off_route: bool
    stale: bool
    updated_at: Time
    sources: list[RealtimeSource]


class TripPart(BaseModel):
    trip_id: str
    run_part: int | None


class TripRef(BaseModel):
    trip_id: str
    service_date: date


class Connection(BaseModel):
    """A connection the timetable promises at one call (JDF návaznosti, CZPTT claims)."""

    direction: Literal["waits_for", "connects_to"]
    at_sequence: int | None
    wait_minutes: int | None
    line: str | None
    destination: str | None
    note: str | None
    trip: TripRef | None = Field(description="The resolved trip, when the claim names one")


class JourneyLink(BaseModel):
    """A train continuing under another number, splitting or joining."""

    kind: Literal["continues_as", "continued_from", "splits_from", "joins"]
    journey: JourneyKey
    trip: TripRef | None


class Trip(Envelope):
    trip_id: str
    service_date: date
    journey: JourneyKey | None
    line: Line
    agency: str | None
    trip_short_name: str | None
    headsign: str | None
    wheelchair_accessible: bool | None
    bikes_allowed: bool | None
    amenities: Amenities
    realtime: TripRealtime | None
    vehicles: list[TripVehicle]
    run_parts: list[TripPart] = Field(description="All parts of a rail run on the date, in order.")
    calls: list[TripCall]
    features: list[Feature]
    notes: list[Note]
    connections: list[Connection]
    links: list[JourneyLink]
    alerts: list[Alert] = Field(
        description="Current alerts of the trip or its line, and the severe ones of its stops"
        " (a stop's others are on its board)"
    )
    shape: dict[str, Any] | None = Field(default=None, description="GeoJSON LineString.")


class CompositionCall(BaseModel):
    sequence: int = Field(description="The trip call's sequence")
    location_id: str
    name: str


class UnitFeature(BaseModel):
    feature: str = Field(
        description="A vehicle attribute (`wifi`, `power_sockets`, `wheelchair_lift`, …) or "
        "`raw:<source>:<code>` for a code with no home in the vocabulary"
    )
    label: Label | None = Field(description="Null for a raw code: its `text` is the source's")
    icon: str | None
    text: str | None = Field(description="The source's own wording of its code")


class CompositionUnit(BaseModel):
    """One vehicle of the train as a source states it: an exact vehicle (`vehicle_id`), a type
    (`vehicle_type`: `Bdmpz 896`), or neither (a locomotive the source does not name). Each unit
    has its own span over the trip's calls: units join and leave en route."""

    source: str
    label: Label | None
    sequence: int = Field(description="Its place in the train as the source orders it, from 1")
    kind: Literal["locomotive", "coach", "unit"]
    role: str = Field(description="train, attached, pusher, or `raw:<code>`")
    vehicle_id: str | None
    vehicle_type: str | None
    coach_number: str | None
    classes: list[int] = Field(description="Travel classes: 1, 2")
    features: list[UnitFeature]
    destination: str | None = Field(description="A through coach's own destination")
    details: dict[str, str] = Field(description="Source-specific facts, kept as given")
    from_call: CompositionCall
    to_call: CompositionCall
    open: bool = Field(
        description="Holds to the end of the trip until a later answer from a later call says "
        "otherwise"
    )
    observed_at: Time


class CompositionSegment(BaseModel):
    """A piece of the trip between span boundaries, with the units running it."""

    from_call: CompositionCall
    to_call: CompositionCall
    units: list[int] = Field(description="Positions in `units`, in train order per source")


class CompositionSource(BaseModel):
    """What a composition source answered to the last request."""

    lookup: str = Field(description="`<source>/<lookup>`")
    source: str
    label: Label | None
    outcome: Literal["answered", "not_found", "not_available", "failed"] | None = Field(
        description="Null while it is still to answer"
    )
    reason: str | None


class Composition(Envelope):
    """A trip's formation (BASE_PLAN.md section 28), asked for lazily: never polled, the API
    never calls upstream. A missing or older answer is asked of the worker's lookup broker:
    `pending` (HTTP 202, `Retry-After`) until a first answer, then `fresh`; an older answer is
    served as `stale` while a newer one is asked. `not_available`: no source answers for this
    trip (another operator's train, a date ČD does not answer for, …), see `sources`."""

    trip_id: str
    service_date: date
    from_location_id: str | None = Field(
        description="The call the answer was asked from; null: the next passenger call"
    )
    status: Literal["fresh", "stale", "pending", "not_available"]
    retry_after_s: int | None
    requested_at: Time | None
    answered_at: Time | None
    sources: list[CompositionSource]
    units: list[CompositionUnit]
    segments: list[CompositionSegment]


class GoneTrip(Problem):
    """A trip ID from an older timetable version: follow `by_key` to today's trip."""

    service_date: date
    journey: JourneyKey
    by_key: str


class Vehicle(BaseModel):
    vehicle_id: str = Field(
        description="The registry's vehicle ID when the source vehicle is bound to one, "
        "else <source>:<source_vehicle_id>; both resolve"
    )
    feed: str
    source: str
    state: VehicleState
    last_seen: Time
    position_at: Time | None = Field(
        default=None,
        description="The source time of the position: a client keeps a newer one it already has",
    )
    latitude: float | None
    longitude: float | None
    bearing: float | None
    trip_id: str | None
    service_date: date | None
    line: Line | None
    trip_short_name: str | None = Field(default=None, description="The train number (Os 6819)")
    headsign: str | None
    delay_s: int | None
    traits: VehicleTraits = Field(default_factory=VehicleTraits)
    sources: list[str] = Field(default_factory=list, description="Sources reporting it now")
    distance_m: int | None = Field(
        default=None, description="Metres from `near`, when a search gives it"
    )


Origin = Literal["curated", "live", "model"]


class VehicleStatement(BaseModel):
    """One statement about an attribute: a registry dataset's, a source's live, or a model's."""

    value: str
    origin: Origin
    source: str = Field(description="The dataset (curated, model) or the realtime source (live)")
    observed_at: Time | None


class VehicleAttributeValue(BaseModel):
    """The value that counts (registry > live by source precedence > model), and all others."""

    attribute: str
    label: Label | None = Field(description="Null for a raw source field")
    icon: str | None
    value: str = Field(description="true/false, digits or text, by the attribute's type")
    origin: Origin
    source: str
    observed_at: Time | None = Field(description="When a source last stated a live value")
    conflict: bool = Field(description="Another curated or live statement says otherwise")
    statements: list[VehicleStatement]


class VehicleSource(BaseModel):
    """One source's report of the vehicle (several when the registry binds them to it)."""

    source: str
    label: Label | None
    source_vehicle_id: str
    state: VehicleState | None
    last_seen: Time | None
    bound_by: Literal["source", "identifier", "co_located", "train"] | None = Field(
        description="What makes this report this vehicle: a registry key (its source's ID, or "
        "an identifier it states: plate, EVN, series number), running one journey side by "
        "side with another source's report of it (provisional), or the train number of the rail "
        "run it is on (`train:<number>`)"
    )


class RegistryEntry(BaseModel):
    vehicle_id: str
    public_label: str | None
    model: str | None
    manufacturer: str | None
    datasets: list[str]


class Vehicles(Envelope):
    vehicles: list[Vehicle]


class VehicleDetail(Envelope):
    vehicle_id: str
    vehicle: Vehicle | None = Field(description="Its current state, if seen lately")
    registry: RegistryEntry | None
    reports: list[VehicleSource] = Field(description="The source reports that are it now")
    sources: list[str] = Field(description="Every source stating its attributes")
    attributes: list[VehicleAttributeValue]


class LineSummary(BaseModel):
    line: Line
    agency: str | None


class Lines(Envelope):
    lines: list[LineSummary]


class PatternStop(BaseModel):
    location_id: str
    name: str
    latitude: float | None
    longitude: float | None


class Pattern(BaseModel):
    """One stop sequence the line runs (passenger calls), with the trips of the date on it."""

    headsign: str | None = Field(description="The most common headsign of its trips")
    direction: int | None = Field(description="The feed's direction (0 or 1), when it has one")
    trips: int
    first_departure: Time | None = Field(description="Of its first trip, at its first stop")
    last_departure: Time | None = Field(description="Of its last trip, at its first stop")
    stops: list[PatternStop]
    shape: dict[str, Any] | None = None


class LineDay(BaseModel):
    """The line on the service date: how many trips run, the first and the last to set off."""

    trips: int
    first_departure: Time | None
    last_departure: Time | None


class LineDetail(Envelope):
    line: Line
    agency: str | None
    description: str | None
    runs_from: date | None = Field(description="The first service date it runs in the release")
    runs_until: date | None = Field(description="The last service date it runs in the release")
    integrated_transport: list[IntegratedSystem]
    features: list[Feature] = Field(description="What the timetable states for the whole line")
    notes: list[Note]
    service_date: date
    day: LineDay
    patterns: list[Pattern]


class TimetableTrip(BaseModel):
    trip_id: str
    trip_short_name: str | None
    headsign: str | None
    wheelchair_accessible: bool | None
    bikes_allowed: bool | None
    times: list[Time | None] = Field(
        description="Scheduled departure at each of the pattern's stops (arrival at the last)"
    )


class TimetablePattern(BaseModel):
    headsign: str | None
    direction: int | None
    stops: list[PatternStop]
    trips: list[TimetableTrip]


class LineTimetable(Envelope):
    line: Line
    service_date: date
    patterns: list[TimetablePattern] = Field(
        description="Per stop sequence, its trips by first departure: a timetable's columns"
    )


class LineTrip(BaseModel):
    trip_id: str
    service_date: date
    trip_short_name: str | None
    headsign: str | None
    first_departure: Time | None
    last_arrival: Time | None
    lifecycle: str | None
    delay_s: int | None
    vehicles: list[TripVehicle] = Field(description="The vehicles on it now")


class LineTrips(Envelope):
    line: Line
    service_date: date
    trips: list[LineTrip]


class HistoryCall(BaseModel):
    location_id: str
    name: str
    scheduled_arrival: Time | None
    scheduled_departure: Time | None
    actual_arrival: Time | None
    actual_departure: Time | None
    arrival_delay_s: int | None
    departure_delay_s: int | None


class HistoryDay(BaseModel):
    service_date: date
    trip_id: str
    cancelled: bool = Field(description="A source cancelled it and no vehicle was seen running it")
    calls: list[HistoryCall]


class TripHistory(Envelope):
    journey: JourneyKey
    days: list[HistoryDay]


class PunctualityBucket(BaseModel):
    key: str = Field(description="The hour (00 to 23) or the stop's location_id")
    name: str | None
    events: int
    on_time_share: float = Field(description="Share within the on-time band of the limits")
    mean_delay_s: float


class LinePunctuality(Envelope):
    line: Line
    since: date
    until: date
    journeys: int
    events: int
    on_time_share: float | None
    by_hour: list[PunctualityBucket]
    by_stop: list[PunctualityBucket]


class SourceHealth(BaseModel):
    source: str
    channel: str
    last_minute: Time
    polls: int
    errors: int
    lag_ms: int | None


class RealtimeFreshness(BaseModel):
    """A realtime source's newest vehicle report in the active release."""

    source: str
    label: Label | None
    last_seen: Time


class Status(Envelope):
    api_version: str
    activated_at: Time
    sources: list[SourceHealth] = Field(description="Polls of the last hour, per channel.")
    realtime: list[RealtimeFreshness] = Field(
        description="Each realtime source's newest vehicle report"
    )
    realtime_at: Time | None = Field(
        default=None,
        description="The newest vehicle report of any source: how live the realtime is",
    )
