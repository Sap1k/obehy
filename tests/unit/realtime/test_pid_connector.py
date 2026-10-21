"""PID connector quirks (docs/sources/pid.md, Quirks)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from obehy.realtime import record
from obehy.realtime.manifest import Auth, Cursor
from obehy.realtime.model import (
    Delay,
    Observation,
    Position,
    SourceState,
    TripCancelled,
    TripKey,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.replay import DECODERS
from obehy.realtime.sources import pid
from obehy.realtime.times import instant

RECEIVED = instant(datetime(2026, 10, 14, 22, 11, 10, tzinfo=UTC))  # 00:11:10 CEST
SKEW = timedelta(minutes=20)
CITY_BUS: dict[str, Any] = {
    "geometry": {"type": "Point", "coordinates": [14.5014257, 50.10096]},
    "properties": {
        "last_position": {
            "bearing": 282,
            "delay": {"actual": -28, "last_stop_arrival": 0, "last_stop_departure": 0},
            "is_canceled": False,
            "origin_timestamp": "2026-10-15T00:11:03+02:00",
            "state_position": "on_track",
            "tracking": True,
        },
        "trip": {
            "cis": {"line_id": None, "trip_number": None},
            "gtfs": {"route_id": "L180", "route_type": 3, "trip_id": "180_2505_260611"},
            "vehicle_registration_number": 6940,
        },
    },
    "type": "Feature",
}


def entry(
    *, trip: dict[str, Any] | None = None, last: dict[str, Any] | None = None
) -> dict[str, Any]:
    feature = copy.deepcopy(CITY_BUS)
    feature["properties"]["trip"].update(trip or {})
    feature["properties"]["last_position"].update(last or {})
    return feature


def decode(*features: dict[str, Any]) -> list[Observation]:
    body = json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()
    return pid.decode(body, "sha", RECEIVED, SKEW)


def test_a_city_vehicle_binds_by_pid_gtfs_trip() -> None:
    (obs,) = decode(entry())
    assert obs.feed == "jdf" and obs.observed_at is not None
    assert obs.all(TripKey) == (TripKey("pid:gtfs_trip_id", "180_2505_260611"),)
    assert obs.first(VehicleKey) == VehicleKey("3:6940")
    assert obs.first(Position) == Position(lat=50.10096, lon=14.5014257, bearing=282.0)
    assert obs.first(SourceState) == SourceState("running")
    assert obs.first(Delay) == Delay(-28, "unknown")


def test_pid_q3_a_regional_bus_also_carries_its_cis_key() -> None:
    (obs,) = decode(entry(trip={"cis": {"line_id": "100380", "trip_number": 1071}}))
    assert obs.all(TripKey) == (
        TripKey("pid:gtfs_trip_id", "180_2505_260611"),
        TripKey("cis:line_trip", "100380:1071"),
    )


def test_pid_q3_trains_go_to_czptt_by_train_number() -> None:
    train = entry(
        trip={
            "cis": {"line_id": "none", "trip_number": 5861},
            "gtfs": {"route_type": 2, "trip_id": "1002_5861_251214"},
            "vehicle_registration_number": None,
        }
    )
    (obs,) = decode(train)
    assert obs.feed == "czptt"
    assert obs.all(TripKey) == (TripKey("czptt:train_number", "5861"),)
    assert obs.first(VehicleKey) == VehicleKey("train:5861")  # PID-Q2


def test_pid_q1_fleet_numbers_are_keyed_per_mode() -> None:
    tram = entry(trip={"gtfs": {"route_type": 0, "trip_id": "9_1_260101"}})
    assert [o.first(VehicleKey) for o in decode(entry(), tram)] == [
        VehicleKey("3:6940"),
        VehicleKey("0:6940"),
    ]


def test_pid_q2_metro_without_a_fleet_number_is_keyed_by_trip() -> None:
    metro = entry(
        trip={
            "gtfs": {"route_type": 1, "trip_id": "991_120_260207"},
            "vehicle_registration_number": None,
        }
    )
    (obs,) = decode(metro)
    assert obs.first(VehicleKey) == VehicleKey("trip:991_120_260207")
    assert pid.mode("trip:991_120_260207", "jdf") == "metro"
    assert pid.mode("0:6940", "jdf") == "tram" and pid.mode("train:5861", "czptt") == "train"


@pytest.mark.parametrize("state", ["before_track", "before_track_delayed"])
def test_pid_q4_before_the_trip_is_pre_trip_without_delay(state: str) -> None:
    (obs,) = decode(entry(last={"state_position": state, "tracking": False}))
    assert obs.first(SourceState) == SourceState("pre_trip")
    assert obs.first(TripKey) is not None and obs.first(Delay) is None


@pytest.mark.parametrize(
    "last", [{"state_position": "after_track"}, {"state_position": "after_track_delayed"}]
)
def test_pid_q4_a_finished_trip_is_not_claimed(last: dict[str, Any]) -> None:
    (obs,) = decode(entry(last=last))
    assert obs.first(TripKey) is None and obs.first(SourceState) is None
    assert obs.first(VehicleKey) == VehicleKey("3:6940") and obs.first(Position) is not None


@pytest.mark.parametrize(
    "fleet_number", [None, 6940], ids=["listed without a vehicle", "with a vehicle"]
)
def test_pid_q4_a_cancelled_trip_is_a_cancellation_without_a_vehicle(
    fleet_number: int | None,
) -> None:
    """2026-10-15: ten cancelled trips, each listed once or twice near its start, `canceled`
    and `is_canceled`, no fleet number, not tracked."""

    cancelled = entry(
        trip={
            "cis": {"line_id": "100394", "trip_number": 1028},
            "gtfs": {"route_type": 3, "trip_id": "394_703_260316"},
            "vehicle_registration_number": fleet_number,
        },
        last={"state_position": "canceled", "is_canceled": True, "tracking": False},
    )
    (obs,) = decode(cancelled, entry())[:1]
    assert obs.first(VehicleKey) is None and obs.first(Position) is None
    assert obs.facts == (
        TripKey("pid:gtfs_trip_id", "394_703_260316"),
        TripKey("cis:line_trip", "100394:1028"),
        TripCancelled(),
    )
    # The vehicle's own entry is still its own observation.
    assert [o.first(VehicleKey) for o in decode(cancelled, entry())] == [None, VehicleKey("3:6940")]


def block_trip(trip_id: str, state: str, start: str) -> dict[str, Any]:
    return entry(
        trip={
            "gtfs": {"route_type": 3, "trip_id": trip_id},
            "start_timestamp": f"2026-10-15T{start}",
        },
        last={"state_position": state, "tracking": state in pid.RUNNING},
    )


def trip_of(obs: Observation) -> str | None:
    key = obs.first(TripKey)
    return None if key is None else key.key


def test_pid_q6_a_vehicle_listed_per_block_trip_keeps_the_one_it_runs() -> None:
    (obs,) = decode(
        block_trip("52_801", "after_track", "00:01:00+02:00"),
        block_trip("52_376", "before_track_delayed", "01:06:00+02:00"),
        block_trip("52_802", "on_track", "00:24:00+02:00"),
        block_trip("52_375", "before_track_delayed", "00:54:00+02:00"),
    )
    assert trip_of(obs) == "52_802" and obs.raw.item == 2
    assert obs.first(SourceState) == SourceState("running")


def test_pid_q6_between_trips_the_next_one_is_pre_trip() -> None:
    # Across the DST change 02:30+02:00 starts before 02:10+01:00.
    (obs,) = decode(
        block_trip("late", "before_track", "02:10:00+01:00"),
        block_trip("done", "after_track", "01:40:00+02:00"),
        block_trip("next", "before_track", "02:30:00+02:00"),
    )
    assert trip_of(obs) == "next" and obs.first(SourceState) == SourceState("pre_trip")
    (obs,) = decode(block_trip("a", "after_track", "01:00:00+02:00"))
    assert trip_of(obs) is None and obs.first(VehicleKey) == VehicleKey("3:6940")


def test_pid_q6_a_handover_keeps_the_trip_on_its_route() -> None:
    (obs,) = decode(
        block_trip("old", "off_track", "04:15:00+02:00"),
        block_trip("new", "at_stop", "04:29:00+02:00"),
    )
    assert trip_of(obs) == "new"


def test_pid_q6_two_trips_running_at_once_bind_neither() -> None:
    (obs,) = decode(
        block_trip("715_370", "on_track", "04:18:00+02:00"),
        block_trip("345_393", "on_track", "04:53:00+02:00"),
    )
    assert trip_of(obs) is None and obs.first(SourceState) is None
    assert obs.first(Position) is not None


def test_t15_a_fix_far_from_reception_has_no_time_or_position() -> None:
    (obs,) = decode(entry(last={"origin_timestamp": "2026-10-14T22:00:00+02:00"}))
    assert obs.observed_at is None and obs.first(Position) is None
    assert obs.first(TripKey) is not None


def test_entries_without_a_mode_or_vehicle_are_skipped() -> None:
    no_mode = entry(trip={"gtfs": {"trip_id": "x"}})
    no_vehicle = entry(
        trip={"gtfs": {"route_type": 3, "trip_id": None}, "vehicle_registration_number": None}
    )
    assert decode(no_mode, no_vehicle) == []


def test_a_body_without_features_is_a_payload_error() -> None:
    with pytest.raises(pid.PayloadError):
        pid.decode(b'{"type": "FeatureCollection"}', "sha", RECEIVED, SKEW)
    with pytest.raises(pid.PayloadError):
        pid.decode(b"<html>", "sha", RECEIVED, SKEW)


def test_pid_is_live_on_both_feeds_with_its_key_and_cursor() -> None:
    (channel,) = [
        c for c in record.load_channels() if c.source == "pid" and c.channel == "vehicles"
    ]
    assert (channel.source, channel.channel) in DECODERS
    assert channel.feeds == ("jdf", "czptt") and channel.live_feeds == ("jdf", "czptt")
    assert channel.auth == Auth("X-Access-Token", "golemio")
    assert channel.cursor == Cursor("x-last-modified", "updatedSince", 120.0)


def test_the_vehicle_says_what_it_is_and_who_runs_it() -> None:
    trip = {
        "wheelchair_accessible": True,
        "air_conditioned": False,
        "usb_chargers": True,
        "agency_name": {"real": "ARRIVA CITY", "scheduled": "DP PRAHA"},
        "vehicle_type": {"id": 3, "description_cs": "autobus", "description_en": "bus"},
    }
    (obs,) = decode(entry(trip=trip))
    assert obs.all(VehicleAttribute) == (
        VehicleAttribute("wheelchair_accessible", "true"),
        VehicleAttribute("air_conditioning", "false"),
        VehicleAttribute("usb", "true"),
        VehicleAttribute("operator", "ARRIVA CITY"),
        VehicleAttribute("category", "autobus"),
        VehicleAttribute("fleet_number", "6940"),
    )
