"""DÚK connector: `GetTraffic` payloads → observations (docs/sources/duk.md).

Quirks normalized here: DUK-Q1 (some units apply the local offset twice to the GPS time), DUK-Q2
(1970 means none), DUK-Q5 (states 2/3 are pre-trip), DUK-Q7 (the prefixed ID is the vehicle
key), DUK-Q8 (`CISLineID = 0` entries are trains keyed by train number), DUK-Q9 (CIS line padded
to 6 digits), DUK-Q13 (an Azimut of 0 is no bearing). Source times further than the policy skew
from reception are dropped (T15). `TODepartureDT` is kept as the source's planned departure
(DUK-Q18).
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
    SourceDeparture,
    SourceState,
    TripKey,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.times import Instant, instant, read_unless_offset_twice, within_skew

SOURCE = "duk"
CHANNEL = "vehicles"
DECODER_VERSION = 4

OFF = 255
PRE_TRIP_STATES = frozenset({2, 3})
TEPLICE = range(400000, 410000)
# DUK-Q1: how far a doubled offset may miss the vehicle's activity time (measured: under
# 20 s, also while both clocks are frozen at a stop).
DOUBLE_OFFSET_TOLERANCE = timedelta(minutes=5)


class PayloadError(ValueError):
    """A successful poll whose body does not have the documented shape."""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _time(value: object) -> Instant | None:
    if not isinstance(value, str) or not value:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        return None
    parsed = instant(moment)
    return None if parsed.year <= 1970 else parsed  # DUK-Q2


def _gps_time(entry: dict[str, Any]) -> Instant | None:
    """The GPS fix time; DUK-Q1: a time later than the unit's own activity time by exactly its
    label's offset had the offset applied twice (Teplice and RJÚK units), whether the vehicle
    moves or both clocks are frozen at a stop."""

    gps = _time(entry.get("GPSPositionDT"))
    activity = _time(entry.get("LastActivityDT"))
    if gps is None or activity is None:
        return gps
    return read_unless_offset_twice(entry["GPSPositionDT"], activity, DOUBLE_OFFSET_TOLERANCE)


def decode(
    body: bytes, sha256: str, received_at: Instant, max_clock_skew: timedelta
) -> list[Observation]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadError(f"not JSON: {error}") from error
    entries = (
        cast(dict[str, Any], document).get("VehicleList") if isinstance(document, dict) else None
    )
    if not isinstance(entries, list):
        raise PayloadError("payload lacks VehicleList")
    observations: list[Observation] = []
    for item, raw in enumerate(cast(list[object], entries)):
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, Any], raw)
        observation = _entry(entry, RawRef(sha256, item), received_at, max_clock_skew)
        if observation is not None:
            observations.append(observation)
    return observations


def _entry(
    entry: dict[str, Any], raw: RawRef, received_at: Instant, max_clock_skew: timedelta
) -> Observation | None:
    try:
        state = int(entry["State"])
        vehicle = int(entry["ID"])
        cis_line = int(entry["CISLineID"])
        trip = int(entry["RouteID"])
    except (KeyError, TypeError, ValueError):
        return None
    if state == OFF:
        return None
    feed: Feed
    if cis_line == 0:
        feed, key = "czptt", TripKey("czptt:train_number", str(trip))
    else:
        feed, key = "jdf", TripKey("cis:line_trip", f"{cis_line:06d}:{trip}")
    observed = _gps_time(entry)
    if observed is not None and not within_skew(observed, received_at, max_clock_skew):
        observed = None
    facts: list[Fact] = [VehicleKey(str(vehicle)), key]
    lat, lon = _number(entry.get("Latitude")), _number(entry.get("Longitude"))
    # A position without a trustworthy time cannot place events in time: it is dropped.
    if observed is not None and lat is not None and lon is not None and lat != 0 and lon != 0:
        bearing = _number(entry.get("Azimut"))
        # DUK-Q13: an Azimut of exactly 0 means unknown, often while the vehicle moves.
        facts.append(Position(lat=lat, lon=lon, bearing=None if bearing == 0 else bearing))
    delay = entry.get("Delay")
    if isinstance(delay, int) and not isinstance(delay, bool):
        facts.append(Delay(delay * 60, "unknown"))
    facts.append(SourceState("pre_trip" if state in PRE_TRIP_STATES else "running"))
    for field, attribute in (
        ("HasLowfloor", "low_floor"),
        ("isAirConditioned", "air_conditioning"),
    ):
        value = entry.get(field)
        if isinstance(value, bool):
            facts.append(VehicleAttribute(attribute, "true" if value else "false"))
    departure = _time(entry.get("TODepartureDT"))
    if departure is not None:
        facts.append(SourceDeparture(departure))
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


def fleet(vehicle: str, feed: Feed) -> str:
    """The DÚK fleet a vehicle key belongs to, for replay reports (docs/sources/duk.md)."""

    if feed == "czptt":
        return "train"
    number = int(vehicle) if vehicle.isdigit() else -1
    if number in TEPLICE:
        return "teplice"
    if 300000 <= number <= 309999:
        return "dpmul"
    return "duk"
