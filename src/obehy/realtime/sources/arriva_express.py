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

from obehy.realtime.facts import (
    Delay,
    Destination,
    Fact,
    LineRef,
    NextStopName,
    Observation,
    RawRef,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.sources.decoding import PayloadError, fix, fresh, integer, load_json, number
from obehy.realtime.times import Instant, read_digits_as_local

SOURCE = "arriva-express"
CHANNEL = "buses"
DECODER_VERSION = 2

EXPRESS = "ARRIVA EXPRESS"  # `mainType` (ARRIVA-Q7)


def recorded(body: bytes) -> tuple[bytes, int, int]:
    """The poll as archived (ARRIVA-Q7): only `mainType = "ARRIVA EXPRESS"` vehicles of the
    GraphQL batch response; with the counts kept and dropped."""

    document = load_json(body)
    if not isinstance(document, list):
        raise PayloadError("expected a GraphQL batch (JSON array)")
    kept = dropped = 0
    for item in cast(list[object], document):
        data = cast(dict[str, Any], item).get("data") if isinstance(item, dict) else None
        if not isinstance(data, dict):
            raise PayloadError("batch item without a data object")
        vehicles = cast(dict[str, Any], data).get("busesCurrentLocations")
        if not isinstance(vehicles, list):
            raise PayloadError("data.busesCurrentLocations is not a list")
        entries = cast(list[object], vehicles)
        express: list[object] = [
            vehicle
            for vehicle in entries
            if isinstance(vehicle, dict)
            and cast(dict[str, Any], vehicle).get("mainType") == EXPRESS
        ]
        kept += len(express)
        dropped += len(entries) - len(express)
        cast(dict[str, Any], data)["busesCurrentLocations"] = express
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, kept, dropped


def decode(
    body: bytes, sha256: str, received_at: Instant, max_clock_skew: timedelta
) -> list[Observation]:
    document = load_json(body)
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
    observed = fresh(_time(entry.get("updated"), received_at), received_at, max_clock_skew)
    facts: list[Fact] = [
        VehicleKey(plate),
        VehicleAttribute("licence_plate", plate),
        LineRef("cis:line", line),
        Destination(destination),
    ]
    following = _text(entry.get("lastStopName"))  # ARRIVA-Q2: the next stop
    if following:
        facts.append(NextStopName(following))
    lat, lon = number(entry.get("latitude")), number(entry.get("longitude"))
    # A position without a trustworthy time cannot place events in time: it is dropped.
    position = fix(lat, lon, number(entry.get("angle")))
    if observed is not None and position is not None:
        facts.append(position)
    minutes = integer(entry.get("delay"))
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


def _time(value: object, received_at: Instant) -> Instant | None:
    """ARRIVA-Q1: `2026-10-05T21:23:31.000+00:00` is 21:23:31 Prague time."""

    if not isinstance(value, str):
        return None
    try:
        return read_digits_as_local(value, received_at)
    except ValueError:
        return None
