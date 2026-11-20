"""MPV net connector: regional `mapData` payloads → observations (docs/sources/mpvnet.md).

One decoder for every portal (IDOL, JIKORD, ODIS, IDZK). Quirks normalized here: MPVNET-Q2 (the
trip is a pipe-separated record, laid out per vehicle kind; no record means no trip), MPVNET-Q3
(city buses report courses, not trips: dropped), MPVNET-Q4 (`dt`/`tm` are local without an
offset), MPVNET-Q5 (`x` is the latitude), MPVNET-Q6 (a train's delay is the signed seconds of
`ds`; its letter is not a sign), MPVNET-Q9 (a bus's current delay is field 6 of its record; `ds`
lags it). Source times further than the policy skew from reception are dropped (T15).

The current delay is measured against the timetable interpolated between stops, not at a stop,
so it is read as `unknown`, a weak constraint; GPS progress decides.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, cast

from obehy.realtime.facts import (
    Delay,
    Fact,
    Feed,
    Observation,
    RawRef,
    TripKey,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.sources.decoding import PayloadError, fix, fresh, line_trip, load_json, number
from obehy.realtime.times import Instant, read_local

SOURCE = "mpvnet"
CHANNELS = ("idol", "jikord", "odis", "zlin")
DECODER_VERSION = 3

BUS, TRAIN = "H", "T"
BUS_FIELDS, TRAIN_FIELDS = 22, 12
REGIONAL_BUS = 2  # `cat`; 3 is a city bus (MPVNET-Q3)
DELAY = re.compile(r"^\$[zp](-?)(\d+)~min\.~(\d+)~s$")  # MPVNET-Q6
BEARING = re.compile(r"^a_(\d{1,3})\.png$")  # `a_stoji.png` is standing: no bearing
CIS_LINE = re.compile(r"^\d{6}$")


def decode(
    body: bytes, sha256: str, received_at: Instant, max_clock_skew: timedelta, channel: str
) -> list[Observation]:
    document = load_json(body)
    entries = (
        cast(dict[str, Any], document).get("transmitters") if isinstance(document, dict) else None
    )
    if not isinstance(entries, list):
        raise PayloadError("payload lacks transmitters")
    observations: list[Observation] = []
    for item, raw in enumerate(cast(list[object], entries)):
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, Any], raw)
        observation = _entry(entry, channel, RawRef(sha256, item), received_at, max_clock_skew)
        if observation is not None:
            observations.append(observation)
    return observations


def _entry(
    entry: dict[str, Any],
    channel: str,
    raw: RawRef,
    received_at: Instant,
    max_clock_skew: timedelta,
) -> Observation | None:
    record = entry.get("cn")
    if not isinstance(record, str):
        return None  # MPVNET-Q2: no trip
    fields = record.split("|")
    kind = entry.get("t")
    feed: Feed
    if kind == BUS and len(fields) == BUS_FIELDS:
        if entry.get("cat") != REGIONAL_BUS or not CIS_LINE.match(fields[0]):
            return None  # MPVNET-Q3
        plate = fields[11].strip()
        if not plate or not fields[2].isdigit():
            return None
        feed = "jdf"
        vehicle = plate
        key = line_trip(fields[0], fields[2])
        delay = _seconds(fields[6])  # MPVNET-Q9
    elif kind == TRAIN and len(fields) == TRAIN_FIELDS:
        if not fields[2].isdigit():
            return None
        feed = "czptt"
        # No unit identity: the train is its own vehicle (one per number and portal day).
        vehicle = f"train:{fields[2]}"
        key = TripKey("czptt:train_number", fields[2])
        delay = _delay(entry.get("ds"))
    else:
        return None
    observed = fresh(_time(entry, received_at), received_at, max_clock_skew)
    facts: list[Fact] = [VehicleKey(vehicle), key]
    if feed == "jdf":
        facts.append(VehicleAttribute("licence_plate", vehicle))
    lat, lon = number(entry.get("x")), number(entry.get("y"))  # MPVNET-Q5
    # A position without a trustworthy time cannot place events in time: it is dropped.
    position = fix(lat, lon, _bearing(entry.get("ico")))
    if observed is not None and position is not None:
        facts.append(position)
    if delay is not None:
        facts.append(Delay(delay, "unknown"))
    return Observation(
        source=SOURCE,
        channel=channel,
        feed=feed,
        received_at=received_at,
        observed_at=observed,
        raw=raw,
        decoder_version=DECODER_VERSION,
        facts=tuple(facts),
    )


def _time(entry: dict[str, Any], received_at: Instant) -> Instant | None:
    """MPVNET-Q4: `dd.mm.yyyy` and `HH:MM:SS`, local, resolved against reception."""

    day, clock = entry.get("dt"), entry.get("tm")
    if not isinstance(day, str) or not isinstance(clock, str):
        return None
    return read_local(f"{day} {clock}", "%d.%m.%Y %H:%M:%S", received_at)


def _seconds(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _delay(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = DELAY.match(value)
    if match is None:
        return None
    seconds = int(match.group(2)) * 60 + int(match.group(3))
    return -seconds if match.group(1) else seconds


def _bearing(value: object) -> float | None:
    match = BEARING.match(value) if isinstance(value, str) else None
    return float(match.group(1)) if match is not None else None
