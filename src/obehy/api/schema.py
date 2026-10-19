"""Public response models: the OpenAPI contract the frontend's types are generated from.

Fields are only ever added (BASE_PLAN.md section 30). Trips are identified by their feed
`trip_id` and service date; journey keys are attributes. Times are ISO 8601 in Europe/Prague.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

CallStatus = Literal["scheduled", "predicted", "actual", "inferred", "no_realtime", "cancelled"]
SourceClass = Literal["gps", "source", "propagated"]
VehicleState = Literal["running", "positioning", "layover", "unmatched", "not_in_service"]


class Envelope(BaseModel):
    release_id: str = Field(description="The active release the answer was read from.")
    generated_at: datetime


class Problem(BaseModel):
    """An error (RFC 9457 problem details)."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None


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
    boarding_points: list[BoardingPoint]
    lines: list[Line]


class Departure(BaseModel):
    trip_id: str
    service_date: date
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


class Departures(Envelope):
    stop: Stop
    since: datetime
    until: datetime
    departures: list[Departure]


class JourneyKey(BaseModel):
    namespace: str
    key: str


class Note(BaseModel):
    kind: str
    label: str | None
    text: str | None


class Feature(BaseModel):
    scope: str
    kind: str
    call_sequence: int | None
    call_sequence_to: int | None
    source_code: str | None
    note: Note | None


class TripCall(BaseModel):
    sequence: int
    location_id: str
    name: str
    latitude: float | None
    longitude: float | None
    pickup_type: int
    dropoff_type: int
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


class Trip(Envelope):
    trip_id: str
    service_date: date
    journey: JourneyKey | None
    line: Line
    agency: str | None
    trip_short_name: str | None
    headsign: str | None
    wheelchair_accessible: int | None
    bikes_allowed: int | None
    realtime: TripRealtime | None
    run_parts: list[TripPart] = Field(description="All parts of a rail run on the date, in order.")
    calls: list[TripCall]
    features: list[Feature]
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
