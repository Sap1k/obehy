"""SŽ train map connector: `OsVlaky` payloads → observations (docs/sources/sz.md).

Quirks normalized here: SZ-Q1 (positions in S-JTSK / Křovák, EPSG:5514), SZ-Q2 (bare `HH:mm`
times, resolved near the response time `md`, itself local), SZ-Q3 (`cr` is the arrival while
standing, else the departure or passage), SZ-Q4 (points by `NÁZEV20` name: every code the SR70
catalogue gives the name; 6-digit codes lose their check digit), SZ-Q5 (the train number is a
further key, used only when the TR is ambiguous), SZ-Q7 (the next point may be an operational
point), SZ-Q8 (`a` is `""` when standing). SZ-Q6 (unchanged entries) is the core's, declared
in the manifest.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import date, time, timedelta
from functools import cache
from pathlib import Path
from typing import Any, cast

from pyproj import Transformer

from obehy.realtime.model import (
    Delay,
    Fact,
    Interval,
    NextPoint,
    NextStopPrediction,
    Observation,
    PointEvent,
    Position,
    RawRef,
    ServiceDay,
    TripKey,
    TripStatus,
    VehicleKey,
)
from obehy.realtime.times import Instant, read_local, resolve_clock, within_skew

SOURCE = "sz-mapa"
CHANNEL = "trains"
DECODER_VERSION = 1

ID = re.compile(r"^TR/([^/]+)/([^/]+)/([^/]+)/([^/]+)/(\d{8})$")
HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
MINUTE = timedelta(seconds=59)  # `cr` is truncated to the minute
# The SR70 catalogue with its 20-character names (jrunify-ext-geodata rail/SR70_Nazev20.csv).
CATALOGUE = Path(__file__).resolve().parents[2] / "data" / "realtime" / "sr70-name20.csv"


class PayloadError(ValueError):
    """A successful poll whose body does not have the documented shape."""


@cache
def _krovak() -> Transformer:
    return Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)


@cache
def name_codes() -> dict[str, tuple[str, ...]]:
    """`NÁZEV20` name → the 5-digit SR70 codes the catalogue gives it (SZ-Q4)."""

    found: dict[str, set[str]] = defaultdict(set)
    with CATALOGUE.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) >= 2 and len(row[0]) >= 5 and row[1].strip():
                found[row[1].strip()].add(row[0][:5])
    return {name: tuple(sorted(codes)) for name, codes in found.items()}


def sr70_5(value: object) -> str | None:
    """A 5- or 6-digit SR70 code as its 5-digit form (without the check digit)."""

    if not isinstance(value, str) or not value.isdigit() or len(value) not in (5, 6):
        return None
    return value[:5]


def _clock(value: object, reference: Instant) -> Instant | None:
    if not isinstance(value, str):
        return None
    match = HHMM.match(value.strip())
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return resolve_clock(time(hours, minutes), reference)


def _md(document: dict[str, Any], received_at: Instant, skew: timedelta) -> Instant | None:
    value = document.get("md")
    if not isinstance(value, str):
        return None
    moment = read_local(value, "%d.%m.%Y %H:%M:%S", received_at)
    if moment is None or not within_skew(moment, received_at, skew):
        return None
    return moment


def decode(
    body: bytes, sha256: str, received_at: Instant, max_clock_skew: timedelta
) -> list[Observation]:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadError(f"not JSON: {error}") from error
    if not isinstance(document, dict):
        raise PayloadError("payload is not an object")
    payload = cast(dict[str, Any], document)
    features = payload.get("result")
    if not isinstance(features, list):
        raise PayloadError("payload lacks result")
    clock = _md(payload, received_at, max_clock_skew)
    reference = clock or received_at
    observations: list[Observation] = []
    for item, raw in enumerate(cast(list[object], features)):
        if not isinstance(raw, dict):
            continue
        observation = _train(
            cast(dict[str, Any], raw), RawRef(sha256, item), received_at, clock, reference
        )
        if observation is not None:
            observations.append(observation)
    return observations


def _train(
    feature: dict[str, Any],
    raw: RawRef,
    received_at: Instant,
    clock: Instant | None,
    reference: Instant,
) -> Observation | None:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return None
    p = cast(dict[str, Any], properties)
    identity = p.get("id")
    if not isinstance(identity, str):
        return None
    match = ID.match(identity)
    if match is None:
        return None
    company, core, variant, year, day = match.groups()
    try:
        operating = date(int(day[:4]), int(day[4:6]), int(day[6:]))
    except ValueError:
        return None
    facts: list[Fact] = []
    facts.append(VehicleKey(identity))
    facts.append(TripKey("czptt:tr", f"Tr:{company}:{core}:{variant}:{year}"))
    number = p.get("tn")
    if isinstance(number, str) and number.isdigit():
        facts.append(TripKey("czptt:train_number", number))  # SZ-Q5 fallback
    facts.append(ServiceDay(operating))
    position = _position(feature, p)
    if position is not None:
        facts.append(position)
    point = _point(p, reference)
    if point is not None:
        facts.append(point)
    next_name = p.get("nna")
    if isinstance(next_name, str) and next_name.strip():
        facts.append(NextPoint(next_name.strip(), sr70_5(p.get("zst_sr70"))))
    delay = p.get("de")
    if isinstance(delay, int) and not isinstance(delay, bool):
        facts.append(Delay(delay * 60, "point"))
    stop = sr70_5(p.get("nsn70"))
    if stop is not None:
        facts.append(
            NextStopPrediction(
                stop, _clock(p.get("nst"), reference), _clock(p.get("nsp"), reference)
            )
        )
    facts.append(TripStatus(p.get("s") == 1, p.get("di") == 1))
    return Observation(
        source=SOURCE,
        channel=CHANNEL,
        feed="czptt",
        received_at=received_at,
        observed_at=clock,
        raw=raw,
        decoder_version=DECODER_VERSION,
        facts=tuple(facts),
    )


def _position(feature: dict[str, Any], p: dict[str, Any]) -> Position | None:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return None
    coordinates = cast(dict[str, Any], geometry).get("coordinates")
    if not isinstance(coordinates, list) or len(cast(list[object], coordinates)) != 2:
        return None
    x, y = cast(list[object], coordinates)
    if not isinstance(x, int | float) or not isinstance(y, int | float) or x == 0 or y == 0:
        return None
    lon, lat = _krovak().transform(float(x), float(y))  # SZ-Q1
    bearing = p.get("a")  # SZ-Q8: "" when standing
    heading = (
        float(bearing)
        if isinstance(bearing, int | float) and not isinstance(bearing, bool)
        else None
    )
    return Position(lat=round(lat, 6), lon=round(lon, 6), bearing=heading)


def _point(p: dict[str, Any], reference: Instant) -> PointEvent | None:
    name = p.get("cna")
    if not isinstance(name, str) or not name.strip():
        return None
    actual = _clock(p.get("cr"), reference)
    if actual is None:
        return None
    name = name.strip()
    return PointEvent(
        name,
        name_codes().get(name, ()),
        _clock(p.get("cp"), reference),
        Interval(actual, Instant(actual + MINUTE)),
        p.get("rr") == 1,
    )
