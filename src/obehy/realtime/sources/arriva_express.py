"""Arriva Express connector: the filtered GraphQL batch → observations
(docs/sources/arriva-express.md).

There is no trip key (ARRIVA-Q4): an observation carries the line, the destination and the next
stop by name, which `infer/keyless` matches against candidate trips. Quirks normalized here:
ARRIVA-Q1 (`updated` is local time labelled `+00:00`: the label is dropped and the time resolved
against reception), ARRIVA-Q2 (`lastStopName` is the next stop), ARRIVA-Q3 (the delay is signed
whole minutes, rounded down, at the departure from the stop before the next one; the channel
declares `delay_floor_minute`), ARRIVA-Q5 (the plate is padded), ARRIVA-Q7 (the recorder keeps
Arriva Express only; anything else is dropped here too). Source times further than the policy
skew from reception are dropped (T15).

`state` (moving / at a stop) is not a trip state, so it is not passed on as `SourceState`, which
the core reads as one; it waits for a stop-ordering fact (BASE_PLAN.md 19.1 `CurrentStop`).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

from obehy.realtime.model import (
    Delay,
    Destination,
    Fact,
    LineRef,
    NextStopName,
    Observation,
    Position,
    RawRef,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.times import Instant, read_local, within_skew

SOURCE = "arriva-express"
CHANNEL = "buses"
DECODER_VERSION = 2

EXPRESS = "ARRIVA EXPRESS"  # `mainType` (ARRIVA-Q7)


class PayloadError(ValueError):
    """A successful poll whose body does not have the documented shape."""


def decode(
    body: bytes, sha256: str, received_at: Instant, max_clock_skew: timedelta
) -> list[Observation]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadError(f"not JSON: {error}") from error
    if not isinstance(document, list) or not document:
        raise PayloadError("payload is not a GraphQL batch")
    first = cast(list[object], document)[0]
    data = cast(dict[str, Any], first).get("data") if isinstance(first, dict) else None
    entries = (
        cast(dict[str, Any], data).get("busesCurrentLocations") if isinstance(data, dict) else None
    )
    if not isinstance(entries, list):
        raise PayloadError("payload lacks busesCurrentLocations")
    observations: list[Observation] = []
    for item, raw in enumerate(cast(list[object], entries)):
        if not isinstance(raw, dict):
            continue
        observation = _entry(
            cast(dict[str, Any], raw), RawRef(sha256, item), received_at, max_clock_skew
        )
        if observation is not None:
            observations.append(observation)
    return observations


def _entry(
    entry: dict[str, Any], raw: RawRef, received_at: Instant, max_clock_skew: timedelta
) -> Observation | None:
    if entry.get("mainType") != EXPRESS:
        return None
    plate, line = _text(entry.get("spz")), _text(entry.get("linkNumber"))
    destination = _text(entry.get("destinationName"))
    if not plate or not line or not destination:
        return None
    observed = _time(entry.get("updated"), received_at)
    if observed is not None and not within_skew(observed, received_at, max_clock_skew):
        observed = None
    facts: list[Fact] = [
        VehicleKey(plate),
        VehicleAttribute("licence_plate", plate),
        LineRef("cis:line", line),
        Destination(destination),
    ]
    following = _text(entry.get("lastStopName"))  # ARRIVA-Q2: the next stop
    if following:
        facts.append(NextStopName(following))
    lat, lon = _number(entry.get("latitude")), _number(entry.get("longitude"))
    # A position without a trustworthy time cannot place events in time: it is dropped.
    if observed is not None and lat is not None and lon is not None and lat != 0 and lon != 0:
        facts.append(Position(lat=lat, lon=lon, bearing=_number(entry.get("angle"))))
    minutes = _integer(entry.get("delay"))
    if minutes is not None:
        facts.append(Delay(minutes * 60, "departure"))  # ARRIVA-Q3
    return Observation(
        source=SOURCE,
        channel=CHANNEL,
        feed="jdf",
        received_at=received_at,
        observed_at=observed,
        raw=raw,
        decoder_version=DECODER_VERSION,
        facts=tuple(facts),
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""  # ARRIVA-Q5


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _integer(value: object) -> int | None:
    try:
        return int(value) if isinstance(value, str | int) and not isinstance(value, bool) else None
    except ValueError:
        return None


def _time(value: object, received_at: Instant) -> Instant | None:
    """ARRIVA-Q1: `2026-10-05T21:23:31.000+00:00` is 21:23:31 Prague time."""

    if not isinstance(value, str) or len(value) < 19:
        return None
    return read_local(value[:19], "%Y-%m-%dT%H:%M:%S", received_at)
