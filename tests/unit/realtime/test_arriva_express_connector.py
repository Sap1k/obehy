"""Arriva Express connector quirks (docs/sources/arriva-express.md, Quirks)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from obehy.realtime import record
from obehy.realtime.model import (
    Delay,
    Destination,
    LineRef,
    NextStopName,
    Observation,
    Position,
    SourceState,
    TripKey,
    VehicleKey,
)
from obehy.realtime.replay import DECODERS
from obehy.realtime.sources import arriva_express
from obehy.realtime.times import instant

RECEIVED = instant(datetime(2026, 10, 5, 19, 58, 0, tzinfo=UTC))  # 21:58 CEST
SKEW = timedelta(minutes=20)


def bus(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "angle": "316",
        "delay": "1",
        "destinationName": "Teplice,Celní",
        "lastStopName": "Teplice,Pražská",
        "latitude": 50.367088317871094,
        "longitude": 14.258370399475098,
        "linkNumber": "580916",
        "state": "v pohybu",
        "type": "Express",
        "mainType": "ARRIVA EXPRESS",
        "spz": "6SA3614   ",
        "updated": "2026-10-05T21:57:38.000+00:00",
        "linkNumberAlias": "AEx",
    }
    base.update(fields)
    return base


def decode(*entries: dict[str, Any]) -> list[Observation]:
    body = json.dumps([{"data": {"busesCurrentLocations": list(entries)}}]).encode()
    return arriva_express.decode(body, "0" * 64, RECEIVED, SKEW)


def test_arriva_q4_no_trip_key_only_line_and_destination() -> None:
    (obs,) = decode(bus())
    assert obs.first(TripKey) is None
    assert obs.first(LineRef) == LineRef("cis:line", "580916")
    assert obs.first(Destination) == Destination("Teplice,Celní")
    assert obs.first(Position) == Position(50.367088317871094, 14.258370399475098, 316.0)
    # The moving / at-stop flag is not a trip state: the core would read it as one.
    assert obs.first(SourceState) is None
    assert (obs.source, obs.channel, obs.feed) == ("arriva-express", "buses", "jdf")


def test_arriva_q1_updated_is_local_time_labelled_utc() -> None:
    (obs,) = decode(bus())
    assert obs.observed_at == instant(datetime(2026, 10, 5, 19, 57, 38, tzinfo=UTC))
    # Read as UTC it would be two hours in the future: beyond the skew the time is dropped,
    # and with it the position.
    (late,) = decode(bus(updated="2026-10-05T23:57:38.000+00:00"))
    assert late.observed_at is None and late.first(Position) is None


def test_arriva_q1_the_repeated_autumn_hour_reads_by_reception() -> None:
    # 2026-10-25 02:30 Prague happens twice: 00:30 UTC (CEST) and 01:30 UTC (CET).
    body = json.dumps(
        [{"data": {"busesCurrentLocations": [bus(updated="2026-10-25T02:30:00.000+00:00")]}}]
    ).encode()
    for received, expected in ((0, 30), (1, 30)):
        at = instant(datetime(2026, 10, 25, received, 31, tzinfo=UTC))
        (obs,) = arriva_express.decode(body, "0" * 64, at, SKEW)
        assert obs.observed_at == instant(datetime(2026, 10, 25, received, expected, tzinfo=UTC))


def test_arriva_q2_last_stop_name_is_the_next_stop() -> None:
    (obs,) = decode(bus())
    assert obs.first(NextStopName) == NextStopName("Teplice,Pražská")
    (unnamed,) = decode(bus(lastStopName=""))
    assert unnamed.first(NextStopName) is None


def test_arriva_q3_delay_is_signed_whole_minutes_at_the_departure() -> None:
    (late, early) = decode(bus(), bus(delay="-7", spz="7AT9086"))
    assert late.first(Delay) == Delay(60, "departure")
    assert early.first(Delay) == Delay(-420, "departure")
    (channel,) = [c for c in record.load_channels() if c.source == "arriva-express"]
    assert channel.core_semantics.delay_floor_minute  # 1 means 60 to 119 s late


def test_arriva_q5_plate_is_padded() -> None:
    (obs,) = decode(bus())
    assert obs.first(VehicleKey) == VehicleKey("6SA3614")


def test_arriva_q7_only_arriva_express_and_an_empty_fleet_is_a_bad_payload() -> None:
    assert decode(bus(mainType="REGIONÁLNÍ DOPRAVA", type="Regional")) == []
    body = json.dumps([{"data": {"busesCurrentLocations": None}}]).encode()
    with pytest.raises(arriva_express.PayloadError):
        arriva_express.decode(body, "0" * 64, RECEIVED, SKEW)


def test_arriva_express_decodes_and_is_live() -> None:
    (channel,) = [c for c in record.load_channels() if c.source == "arriva-express"]
    assert (channel.source, channel.channel) in DECODERS
    assert channel.live_feeds == ("jdf",) and channel.is_live("jdf")
