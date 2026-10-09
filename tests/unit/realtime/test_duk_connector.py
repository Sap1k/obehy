"""DÚK connector quirks (docs/sources/duk.md, Quirks); core-side quirks live in test_binding."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from obehy.realtime.model import Delay, Observation, Position, SourceState, TripKey, VehicleKey
from obehy.realtime.sources import duk
from obehy.realtime.times import instant

RECEIVED = instant(datetime(2026, 10, 5, 19, 58, 2, tzinfo=UTC))  # 21:58 CEST
SKEW = timedelta(minutes=20)


def entry(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ID": 810,
        "Delay": 2,
        "LineID": 486,
        "RouteID": 153,
        "Longitude": 13.87098,
        "Latitude": 50.67947,
        "ArrivalDT": "1970-01-01T02:00:00+02:00",
        "CISLineID": 582486,
        "GPSPositionDT": "2026-10-05T21:57:48+02:00",
        "Azimut": 8.0,
        "State": 0,
    }
    base.update(fields)
    return base


def decode(*entries: dict[str, Any]) -> list[Observation]:
    body = json.dumps({"VehicleList": list(entries)}).encode()
    return duk.decode(body, "f" * 64, RECEIVED, SKEW)


def test_a_bus_becomes_a_keyed_jdf_observation() -> None:
    (obs,) = decode(entry())
    assert obs.feed == "jdf"
    assert obs.first(VehicleKey) == VehicleKey("810")
    assert obs.first(TripKey) == TripKey("cis:line_trip", "582486:153")
    assert obs.first(Position) == Position(lat=50.67947, lon=13.87098, bearing=8.0)
    assert obs.first(Delay) == Delay(120, "unknown")
    assert obs.observed_at == instant(datetime(2026, 10, 5, 19, 57, 48, tzinfo=UTC))
    assert (obs.raw.sha256, obs.raw.item) == ("f" * 64, 0)


def test_duk_q1_teplice_gps_time_digits_are_utc() -> None:
    (obs,) = decode(entry(ID=400132, GPSPositionDT="2026-10-05T19:57:59+02:00"))
    assert obs.observed_at == instant(datetime(2026, 10, 5, 19, 57, 59, tzinfo=UTC))


def test_duk_q2_1970_means_no_time() -> None:
    (obs,) = decode(entry(GPSPositionDT="1970-01-01T02:00:00+02:00"))
    assert obs.observed_at is None and obs.at == RECEIVED
    assert obs.first(Position) is None  # no time, no usable position
    assert obs.first(TripKey) is not None


@pytest.mark.parametrize(
    ("state", "code"), [(0, "running"), (1, "running"), (2, "pre_trip"), (3, "pre_trip")]
)
def test_duk_q5_states_two_and_three_are_pre_trip(state: int, code: str) -> None:
    (obs,) = decode(entry(State=state))
    assert obs.first(SourceState) == SourceState(code)


def test_vehicles_that_are_off_are_skipped() -> None:
    assert decode(entry(State=255)) == []


def test_duk_q7_prefixed_ids_are_the_vehicle_key() -> None:
    (obs,) = decode(entry(ID=300123, CISLineID=585301))
    assert obs.first(VehicleKey) == VehicleKey("300123")


def test_duk_q8_trains_are_keyed_by_train_number_in_czptt() -> None:
    (obs,) = decode(entry(ID=20092, CISLineID=0, RouteID=28015))
    assert obs.feed == "czptt"
    assert obs.first(TripKey) == TripKey("czptt:train_number", "28015")


def test_duk_q9_cis_line_is_padded_to_six_digits() -> None:
    (obs,) = decode(entry(CISLineID=626, RouteID=3))
    assert obs.first(TripKey) == TripKey("cis:line_trip", "000626:3")


def test_t15_a_gps_time_beyond_the_skew_is_dropped() -> None:
    (obs,) = decode(entry(GPSPositionDT="2026-10-05T22:58:00+02:00"))
    assert obs.observed_at is None
    assert obs.first(Position) is None


def test_bad_payloads_raise() -> None:
    with pytest.raises(duk.PayloadError):
        duk.decode(b"{}", "f" * 64, RECEIVED, SKEW)
    with pytest.raises(duk.PayloadError):
        duk.decode(b"<html>", "f" * 64, RECEIVED, SKEW)
