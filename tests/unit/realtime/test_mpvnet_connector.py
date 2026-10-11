"""MPV net connector quirks (docs/sources/mpvnet.md, Quirks)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from obehy.realtime import record
from obehy.realtime.model import Delay, Observation, Position, TripKey, VehicleKey
from obehy.realtime.replay import DECODERS
from obehy.realtime.sources import mpvnet
from obehy.realtime.times import instant

RECEIVED = instant(datetime(2026, 10, 11, 15, 27, 55, tzinfo=UTC))  # 17:27:55 CEST
SKEW = timedelta(minutes=20)
BUS = (
    "500459|459|105|Zákupy,Nádražní|216|Zákupy,nám.|216|1046|Nový Bor,aut.nádr.|"
    "Zákupy,Nové Zákupy sídl.|1|6L19269|0|0|0|0|0|0|0|0|0|0"
)


def bus(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "t": "H",
        "c": 0,
        "dm": 3,
        "ds": "$z3~min.~36~s",
        "cr": "ČSAD Liberec, prov.ČL",
        "x": 50.68153,
        "y": 14.63543,
        "dt": "11.10.2026",
        "tm": "17:27:36",
        "ico": "a_0.png",
        "cn": BUS,
        "cat": 2,
    }
    base.update(fields)
    return base


def train(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "t": "T",
        "c": 1020,
        "dm": -3,
        "ds": "$p-3~min.~00~s",
        "n": "Os 6615",
        "cr": "Die Länderbahn GmbH",
        "x": 50.75916,
        "y": 15.047535,
        "dt": "11.10.2026",
        "tm": "17:26:25",
        "ico": "a_stoji.png",
        "cn": "Os 6615|L2|6615|Liberec|-180|||||Liberec|1|",
        "cat": 1,
    }
    base.update(fields)
    return base


def decode(*entries: dict[str, Any]) -> list[Observation]:
    body = json.dumps({"portal": "Idol", "transmitters": list(entries)}).encode()
    return mpvnet.decode(body, "f" * 64, RECEIVED, SKEW, "idol")


def test_mpvnet_q4_q5_a_regional_bus_becomes_a_keyed_jdf_observation() -> None:
    # MPVNET-Q4 (local time), Q5 (x is the latitude), Q6 (signed seconds).
    (obs,) = decode(bus())
    assert (obs.source, obs.channel, obs.feed) == ("mpvnet", "idol", "jdf")
    assert obs.first(VehicleKey) == VehicleKey("6L19269")
    assert obs.first(TripKey) == TripKey("cis:line_trip", "500459:105")
    assert obs.first(Position) == Position(lat=50.68153, lon=14.63543, bearing=0.0)
    assert obs.first(Delay) == Delay(216, "unknown")
    assert obs.observed_at == instant(datetime(2026, 10, 11, 15, 27, 36, tzinfo=UTC))


def test_mpvnet_q6_early_running_is_negative_whatever_the_letter() -> None:
    (early,) = decode(bus(ds="$p-3~min.~58~s"))
    (on_time,) = decode(bus(ds="$p0~min.~00~s"))
    (just_early,) = decode(bus(ds="$p-0~min.~25~s"))
    assert early.first(Delay) == Delay(-238, "unknown")
    assert on_time.first(Delay) == Delay(0, "unknown")
    assert just_early.first(Delay) == Delay(-25, "unknown")


def test_mpvnet_q3_city_buses_and_entries_without_a_trip_are_dropped() -> None:
    city = bus(
        cat=3,
        cn="8|8|196|||Elektra|-9|1052|Hlavní nádraží|Poruba,vozovna|1|Ev. č. 1765" + "|0" * 10,
    )
    assert decode(city, bus(cn=None, cat=0), bus(cn="1|2|3")) == []


def test_mpvnet_q8_a_train_is_its_own_vehicle_and_standing_has_no_bearing() -> None:
    (obs,) = decode(train())
    assert obs.feed == "czptt"
    assert obs.first(TripKey) == TripKey("czptt:train_number", "6615")
    assert obs.first(VehicleKey) == VehicleKey("train:6615")
    position = obs.first(Position)
    assert position is not None and position.bearing is None
    assert obs.first(Delay) == Delay(-180, "unknown")


def test_mpvnet_q8_an_old_event_time_keeps_the_trip_but_drops_the_position() -> None:
    # Trains often report the time of their last event, 15-20 minutes back.
    (obs,) = decode(train(tm="17:01:00"))
    assert obs.observed_at is None and obs.first(Position) is None
    assert obs.first(TripKey) == TripKey("czptt:train_number", "6615")


def test_every_portal_decodes_but_none_is_live_yet() -> None:
    channels = [c for c in record.load_channels() if c.source == "mpvnet"]
    assert [c.channel for c in channels] == list(mpvnet.CHANNELS)
    assert all((c.source, c.channel) in DECODERS for c in channels)
    assert all(c.live_feeds == () and c.feeds == ("jdf", "czptt") for c in channels)
