"""Public response models: the OpenAPI contract the frontend's types are generated from.

Trips are identified by their feed `trip_id` and service date; journey keys are attributes.
Times are ISO 8601 in Europe/Prague. Static codes (JDF fixed codes, CZPTT KADR codes, GTFS
boarding codes) are given in readable form (`api/codes.py`), never as raw symbols.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

CallStatus = Literal["scheduled", "predicted", "actual", "inferred", "no_realtime", "cancelled"]
SourceClass = Literal["gps", "source", "propagated"]
VehicleState = Literal["running", "positioning", "layover", "unmatched", "not_in_service"]
Boarding = Literal["regular", "none", "phone", "ask_driver"]
WheelchairAmenity = Literal["booking_required", "booking_recommended", "accessible", "partly"]
BicycleAmenity = Literal["prohibited", "reservation_required", "reservation_available", "allowed"]
ReservationAmenity = Literal["required", "available"]


class Envelope(BaseModel):
    release_id: str = Field(description="The active release the answer was read from.")
    generated_at: datetime


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
    label: str | None
    text: str | None


class Feature(BaseModel):
    """A typed timetable feature (a JDF fixed code or CZPTT code), readable."""

    kind: str
    group: str = Field(
        description="accessibility, bicycles, reservation, onboard, operation, stopping, "
        "facility, interchange, calendar, note or integrated_transport"
    )
    label: Label
    icon: str
    scope: str = Field(description="trip, route, service, call, call_range or location")
    from_sequence: int | None = Field(description="First call it applies to; null: all")
    to_sequence: int | None = Field(description="Last call of a call range")
    value: Label | None = Field(default=None, description="A calendar designation's text")
    system: IntegratedSystem | None = None
    note: Note | None = None


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
    lines: list[Line]


class TripVehicle(BaseModel):
    """The vehicle currently on a trip."""

    vehicle_id: str
    state: VehicleState
    last_seen: datetime
    latitude: float | None
    longitude: float | None
    bearing: float | None


class Departure(BaseModel):
    """A board row: a departure (or, on an arrivals board, an arrival) at a stop place."""

    trip_id: str
    service_date: date
    location_id: str = Field(description="The stop place of this row (multi-stop boards)")
    line: Line
    trip_short_name: str | None
    headsign: str | None
    scheduled: datetime
    estimated: datetime | None
    delay_s: int | None
    status: CallStatus
    source_class: SourceClass | None
    stale: bool | None
    platform: str | None = Field(description="The scheduled platform, track or post code.")
    vehicle: TripVehicle | None = None


class Departures(Envelope):
    stop: Stop
    stops: list[Stop] = Field(description="Every stop place of a multi-stop board")
    since: datetime
    until: datetime
    departures: list[Departure]
    next: str | None = Field(description="Cursor for the following rows (`cursor=`)")


class Arrivals(Envelope):
    stop: Stop
    stops: list[Stop]
    since: datetime
    until: datetime
    arrivals: list[Departure]
    next: str | None


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
        description="request_stop, exit_only, boarding_only, not_stopping, "
        "border_control_only, conditional, diversion"
    )
    platform: str | None
    scheduled_arrival: datetime | None
    scheduled_departure: datetime | None
    estimated_arrival: datetime | None
    estimated_departure: datetime | None
    status: CallStatus
    source_class: SourceClass | None


class TripRealtime(BaseModel):
    lifecycle: str
    delay_s: int | None
    off_route: bool
    stale: bool
    updated_at: datetime


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
    shape: dict[str, Any] | None = Field(default=None, description="GeoJSON LineString.")


class GoneTrip(Problem):
    """A trip ID from an older timetable version: follow `by_key` to today's trip."""

    service_date: date
    journey: JourneyKey
    by_key: str


class Vehicle(BaseModel):
    vehicle_id: str
    feed: str
    source: str
    state: VehicleState
    last_seen: datetime
    latitude: float | None
    longitude: float | None
    bearing: float | None
    trip_id: str | None
    service_date: date | None
    line: Line | None
    headsign: str | None
    delay_s: int | None


class Vehicles(Envelope):
    vehicles: list[Vehicle]


class VehicleDetail(Envelope):
    vehicle: Vehicle


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
    """One stop sequence the line runs, with how many trips of the date use it."""

    headsign: str | None
    trips: int
    stops: list[PatternStop]
    shape: dict[str, Any] | None = None


class LineDetail(Envelope):
    line: Line
    agency: str | None
    description: str | None
    service_date: date
    patterns: list[Pattern]


class LineTrip(BaseModel):
    trip_id: str
    service_date: date
    trip_short_name: str | None
    headsign: str | None
    first_departure: datetime | None
    last_arrival: datetime | None
    lifecycle: str | None
    delay_s: int | None


class LineTrips(Envelope):
    line: Line
    service_date: date
    trips: list[LineTrip]


class HistoryCall(BaseModel):
    location_id: str
    name: str
    scheduled_arrival: datetime | None
    scheduled_departure: datetime | None
    actual_arrival: datetime | None
    actual_departure: datetime | None
    arrival_delay_s: int | None
    departure_delay_s: int | None


class HistoryDay(BaseModel):
    service_date: date
    trip_id: str
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
    last_minute: datetime
    polls: int
    errors: int
    lag_ms: int | None


class Status(Envelope):
    api_version: str
    activated_at: datetime
    sources: list[SourceHealth] = Field(description="Polls of the last hour, per channel.")
