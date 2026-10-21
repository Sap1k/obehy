"""PID connector: Golemio `/v2/vehiclepositions` payloads → observations (docs/sources/pid.md).

The channel polls deltas (`updatedSince`, the manifest's cursor): each payload is a GeoJSON
FeatureCollection of the vehicles updated since the last poll, every entry complete, so each
decodes on its own.

Quirks normalized here: PID-Q1 (fleet numbers are unique per mode only: the vehicle key is
`<route_type>:<number>`), PID-Q2 (trains and metro have no fleet number: trains are keyed by
train number, metro by trip), PID-Q3 (`cis.line_id` is `"none"` for trains and null for city
lines), PID-Q4 (`state_position`: before the trip is pre-trip, after it the entry still names the
finished trip, which is dropped; a cancelled trip is listed without a vehicle and becomes a
cancellation of its trip, `TripCancelled`), PID-Q6 (each vehicle is listed once
per trip of its block, all at its one position: only the trip it is running, else its next one,
is kept). Trains go to czptt by train number,
since the static PID overlay leaves rail out; other vehicles bind by PID GTFS trip ID, with the
CIS line and trip as a fallback where Golemio gives them. Fix times further than the policy skew
from reception are dropped (T15).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from obehy.realtime.model import (
    Delay,
    Fact,
    Feed,
    Observation,
    Position,
    RawRef,
    SourceState,
    TripCancelled,
    TripKey,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.times import Instant, instant, within_skew

SOURCE = "pid"
CHANNEL = "vehicles"
DECODER_VERSION = 4

TRAIN = 2  # GTFS route_type
PRE_TRIP = frozenset({"before_track", "before_track_delayed"})
RUNNING = frozenset({"on_track", "at_stop", "off_track"})
OFF_ROUTE = "off_track"
CANCELLED = "canceled"
MODES = {0: "tram", 1: "metro", 2: "train", 3: "bus", 4: "ferry", 7: "funicular", 11: "trolleybus"}


class PayloadError(ValueError):
    """A successful poll whose body does not have the documented shape."""


def _table(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _time(value: object) -> Instant | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if moment.tzinfo is None else instant(moment)


def decode(
    body: bytes, sha256: str, received_at: Instant, max_clock_skew: timedelta
) -> list[Observation]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadError(f"not JSON: {error}") from error
    features = _table(document).get("features")
    if not isinstance(features, list):
        raise PayloadError("payload lacks features")
    blocks: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    observations: list[Observation] = []
    for item, raw in enumerate(cast(list[object], features)):
        feature = _table(raw)
        if _cancelled(feature):  # PID-Q4: the trip, not a vehicle
            found = _cancellation(feature, RawRef(sha256, item), received_at, max_clock_skew)
            observations.extend([] if found is None else [found])
            continue
        vehicle = _vehicle(feature)
        if vehicle is not None:
            blocks.setdefault(vehicle, []).append((item, feature))
    for vehicle, entries in blocks.items():
        item, feature, bound = _current(entries)
        observations.append(
            _observation(feature, vehicle, bound, RawRef(sha256, item), received_at, max_clock_skew)
        )
    observations.sort(key=lambda observation: observation.raw.item)
    return observations


def _cancelled(feature: dict[str, Any]) -> bool:
    last = _table(_table(feature.get("properties")).get("last_position"))
    return last.get("state_position") == CANCELLED or last.get("is_canceled") is True


def _cancellation(
    feature: dict[str, Any], raw: RawRef, received_at: Instant, max_clock_skew: timedelta
) -> Observation | None:
    """PID-Q4: a cancelled trip's entry, listed without a fleet number and not tracked: the
    trip's keys and `TripCancelled`, no vehicle and no position (it is where the trip would
    start)."""

    properties = _table(feature.get("properties"))
    trip = _table(properties.get("trip"))
    route_type = _integer(_table(trip.get("gtfs")).get("route_type"))
    if route_type is None:
        return None
    feed, keys, _ = _keys(trip, route_type)
    if not keys:
        return None
    observed = _time(_table(properties.get("last_position")).get("origin_timestamp"))
    if observed is not None and not within_skew(observed, received_at, max_clock_skew):
        observed = None
    return Observation(
        source=SOURCE,
        channel=CHANNEL,
        feed=feed,
        received_at=received_at,
        observed_at=observed,
        raw=raw,
        decoder_version=DECODER_VERSION,
        facts=(*keys, TripCancelled()),
    )


def _state(feature: dict[str, Any]) -> object:
    return _table(_table(feature.get("properties")).get("last_position")).get("state_position")


def _start(feature: dict[str, Any]) -> datetime:
    """When the entry's trip starts (offsets differ across a DST change); unknown sorts last."""

    start = _time(_table(_table(feature.get("properties")).get("trip")).get("start_timestamp"))
    return start if start is not None else datetime.max.replace(tzinfo=UTC)


def _current(entries: list[tuple[int, dict[str, Any]]]) -> tuple[int, dict[str, Any], bool]:
    """PID-Q6: the entry of a vehicle's block that names what it does now, and whether its trip
    may bind. The running trip wins, one on its route over one off it; two still running at once
    bind neither. Without one, the next trip to start is pre-trip; after the last, none binds."""

    running = [entry for entry in entries if _state(entry[1]) in RUNNING]
    if running:
        on_route = [entry for entry in running if _state(entry[1]) != OFF_ROUTE] or running
        return *on_route[0], len(on_route) == 1
    upcoming = sorted(
        (entry for entry in entries if _state(entry[1]) in PRE_TRIP),
        key=lambda entry: _start(entry[1]),
    )
    return *(upcoming or entries)[0], True


def _keys(trip: dict[str, Any], route_type: int) -> tuple[Feed, list[TripKey], str | None]:
    """The feed, the trip keys in preference order, and the vehicle key when the entry has no
    fleet number (PID-Q2)."""

    cis = _table(trip.get("cis"))
    number = _integer(cis.get("trip_number"))
    if route_type == TRAIN:
        if number is None:
            return "czptt", [], None
        return "czptt", [TripKey("czptt:train_number", str(number))], f"train:{number}"
    keys: list[TripKey] = []
    trip_id = _table(trip.get("gtfs")).get("trip_id")
    if isinstance(trip_id, str) and trip_id:
        keys.append(TripKey("pid:gtfs_trip_id", trip_id))
    line = cis.get("line_id")  # PID-Q3: "none" for trains, null for city lines
    if isinstance(line, str) and line.isdigit() and number is not None:
        keys.append(TripKey("cis:line_trip", f"{int(line):06d}:{number}"))
    fallback = f"trip:{trip_id}" if isinstance(trip_id, str) and trip_id else None
    return "jdf", keys, fallback


def _vehicle(feature: dict[str, Any]) -> str | None:
    trip = _table(_table(feature.get("properties")).get("trip"))
    route_type = _integer(_table(trip.get("gtfs")).get("route_type"))
    if route_type is None:
        return None
    _, _, fallback = _keys(trip, route_type)
    fleet_number = _integer(trip.get("vehicle_registration_number"))
    # PID-Q1: fleet numbers repeat across modes (bus 1332, tram 1332).
    return f"{route_type}:{fleet_number}" if fleet_number is not None else fallback


def _attributes(trip: dict[str, Any]) -> list[VehicleAttribute]:
    """The vehicle's equipment and its real operator, as the trip entry gives them."""

    found: list[VehicleAttribute] = []
    for field, attribute in (
        ("wheelchair_accessible", "wheelchair_accessible"),
        ("air_conditioned", "air_conditioning"),
        ("usb_chargers", "usb"),
    ):
        value = trip.get(field)
        if isinstance(value, bool):
            found.append(VehicleAttribute(attribute, "true" if value else "false"))
    operator = _table(trip.get("agency_name")).get("real")
    if isinstance(operator, str) and operator.strip():
        found.append(VehicleAttribute("operator", operator.strip()))
    category = _table(trip.get("vehicle_type")).get("description_cs")
    if isinstance(category, str) and category.strip():
        found.append(VehicleAttribute("category", category.strip()))
    fleet_number = _integer(trip.get("vehicle_registration_number"))
    if fleet_number is not None:
        found.append(VehicleAttribute("fleet_number", str(fleet_number)))
    return found


def _observation(
    feature: dict[str, Any],
    vehicle: str,
    bound: bool,
    raw: RawRef,
    received_at: Instant,
    max_clock_skew: timedelta,
) -> Observation:
    properties = _table(feature.get("properties"))
    trip = _table(properties.get("trip"))
    last = _table(properties.get("last_position"))
    feed, keys, _ = _keys(trip, cast(int, _table(trip.get("gtfs")).get("route_type")))
    state = last.get("state_position")
    # PID-Q4: an entry after its trip (`after_track*`) names a trip it is not running.
    if not bound or state not in PRE_TRIP | RUNNING:
        keys = []
    observed = _time(last.get("origin_timestamp"))
    if observed is not None and not within_skew(observed, received_at, max_clock_skew):
        observed = None
    facts: list[Fact] = [VehicleKey(vehicle), *keys]
    coordinates = _table(feature.get("geometry")).get("coordinates")
    if observed is not None and isinstance(coordinates, list):
        point = cast(list[object], coordinates)
        lon, lat = (_number(point[0]), _number(point[1])) if len(point) >= 2 else (None, None)
        if lat is not None and lon is not None and lat != 0 and lon != 0:
            facts.append(Position(lat=lat, lon=lon, bearing=_number(last.get("bearing"))))
    facts.extend(_attributes(trip))
    if keys and state in PRE_TRIP:
        facts.append(SourceState("pre_trip"))
    elif keys and state in RUNNING:
        facts.append(SourceState("running"))
        delay = _integer(_table(last.get("delay")).get("actual"))
        if delay is not None:
            facts.append(Delay(delay, "unknown"))
    return Observation(
        source=SOURCE,
        channel=CHANNEL,
        feed=feed,
        received_at=received_at,
        observed_at=observed,
        raw=raw,
        decoder_version=DECODER_VERSION,
        facts=tuple(facts),
    )


def mode(vehicle: str, feed: Feed) -> str:
    """The mode of a vehicle key, for replay reports."""

    if feed == "czptt":
        return "train"
    head = vehicle.split(":", 1)[0]
    if head == "trip":
        return "metro"
    return MODES.get(int(head), "other") if head.isdigit() else "other"
