"""PID connector: Golemio `/v2/vehiclepositions` payloads → observations (docs/sources/pid.md).

The channel polls deltas (`updatedSince`, the manifest's cursor): each payload is a GeoJSON
FeatureCollection of the vehicles updated since the last poll, every entry complete, so each
decodes on its own.

Quirks normalized here: PID-Q1 (fleet numbers are unique per mode only: the vehicle key is
`<route_type>:<number>`), PID-Q2 (trains and metro have no fleet number: trains are keyed by
train number, metro by trip), PID-Q3 (`cis.line_id` is `"none"` for trains and null for city
lines), PID-Q4 (`state_position`: before the trip is pre-trip, after it the entry still names the
finished trip, which is dropped; a cancelled trip likewise). Trains go to czptt by train number,
since the static PID overlay leaves rail out; other vehicles bind by PID GTFS trip ID, with the
CIS line and trip as a fallback where Golemio gives them. Fix times further than the policy skew
from reception are dropped (T15).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast

from obehy.realtime.model import (
    Delay,
    Fact,
    Feed,
    Observation,
    Position,
    RawRef,
    SourceState,
    TripKey,
    VehicleKey,
)
from obehy.realtime.times import Instant, instant, within_skew

SOURCE = "pid"
CHANNEL = "vehicles"
DECODER_VERSION = 1

TRAIN = 2  # GTFS route_type
PRE_TRIP = frozenset({"before_track", "before_track_delayed"})
RUNNING = frozenset({"on_track", "at_stop", "off_track"})
AFTER_TRIP = frozenset({"after_track", "after_track_delayed"})
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
    observations: list[Observation] = []
    for item, raw in enumerate(cast(list[object], features)):
        observation = _feature(_table(raw), RawRef(sha256, item), received_at, max_clock_skew)
        if observation is not None:
            observations.append(observation)
    return observations


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


def _feature(
    feature: dict[str, Any], raw: RawRef, received_at: Instant, max_clock_skew: timedelta
) -> Observation | None:
    properties = _table(feature.get("properties"))
    trip = _table(properties.get("trip"))
    last = _table(properties.get("last_position"))
    route_type = _integer(_table(trip.get("gtfs")).get("route_type"))
    if route_type is None:
        return None
    feed, keys, fallback = _keys(trip, route_type)
    fleet_number = _integer(trip.get("vehicle_registration_number"))
    # PID-Q1: fleet numbers repeat across modes (bus 1332, tram 1332).
    vehicle = f"{route_type}:{fleet_number}" if fleet_number is not None else fallback
    if vehicle is None:
        return None
    state = last.get("state_position")
    # PID-Q4: an entry after its trip, or a cancelled one, names a trip it is not running.
    if state in AFTER_TRIP or last.get("is_canceled") is True:
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
