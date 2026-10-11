"""DPMD connector quirks (docs/sources/dpmd.md, Quirks): raw JavaScript → archived JSON → facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obehy.realtime import record
from obehy.realtime.model import Delay, Observation, Position, TripKey, VehicleKey
from obehy.realtime.replay import DECODERS
from obehy.realtime.sources import dpmd
from obehy.realtime.times import instant

RECEIVED = instant(datetime(2026, 10, 11, 15, 30, 21, tzinfo=UTC))  # 17:30:21 CEST
SKEW = timedelta(minutes=20)


def bus(
    vehicle: str = "842",
    *,
    trip: str = "<br>Linka: 201<br>Spoj: 351",
    speed: str = "25 km/h",
    delay: str = "",
    at: str = "11.10.2026 17:30:03",
    off: int = 0,
) -> str:
    body = (
        f"<br>Čas: {at}<br>Řidič: 1103<br>Autobus: {vehicle}{trip}<br>Turnus: 7115"
        f"<br>Rychlost: {speed}{delay}<br><br>16:50 - Chrochvice<br>&darr;<br>17:44 - Pošta"
        '<br><img src="`+vehicleImageUrl + `842.jpg">'
    )
    return (
        f"{{id:`3f2172e3`,line:`201`,vehicle:`{vehicle}`,driver:`1103`,course:`7115`,"
        f"header:`x`,body:`{body}`,hint:`x`,arrow:`85`,"
        f"coords:`50.78264999N, 14.22869015E`,final1: null, final2: null, shapes:`<gpx></gpx>`,"
        f"off: {off}}}"
    )


def decode(*buses: str, stamp: str = "2026-10-11T17:30:16.391") -> list[Observation]:
    script = (
        f'/* {{"TimeStamp":"{stamp}"}} */ busstops=[]; buspositions=[{",".join(buses)}];'
        " legends=[]; datahash='0b22';"
    ).encode()
    archived, _, _ = dpmd.recorded(script)
    return dpmd.decode(archived, "f" * 64, RECEIVED, SKEW)


def test_dpmd_q1_a_bus_on_a_trip_becomes_a_keyed_jdf_observation() -> None:
    (obs,) = decode(bus())
    assert (obs.source, obs.channel, obs.feed) == ("dpmd", "vehicles", "jdf")
    assert obs.first(VehicleKey) == VehicleKey("842")
    assert obs.first(TripKey) == TripKey("cis:line_trip", "515201:351")
    assert obs.first(Position) == Position(lat=50.78264999, lon=14.22869015, bearing=85.0)
    assert obs.observed_at == instant(datetime(2026, 10, 11, 15, 30, 3, tzinfo=UTC))


def test_dpmd_q4_no_delay_shown_is_zero_and_minutes_are_seconds() -> None:
    (on_time,) = decode(bus())
    (late,) = decode(bus(delay="<br>Zpoždění: 4 min."))
    assert on_time.first(Delay) == Delay(0, "unknown")
    assert late.first(Delay) == Delay(240, "unknown")


def test_dpmd_q5_parked_and_tripless_vehicles_give_nothing() -> None:
    assert decode(bus(off=1), bus("900", trip="")) == []


def test_dpmd_q8_the_driver_never_reaches_the_archive() -> None:
    script = (
        '/* {"TimeStamp":"2026-10-11T17:30:16"} */ buspositions=[' + bus() + "]; datahash='x';"
    ).encode()
    archived, kept, _ = dpmd.recorded(script)
    assert kept == 1 and b"1103" not in archived


def test_dpmd_q9_a_stale_cached_copy_is_skipped() -> None:
    assert decode(bus(), stamp="2026-10-11T17:05:16.391") == []


def test_a_standing_bus_has_no_bearing() -> None:
    (obs,) = decode(bus(speed="0 km/h"))
    position = obs.first(Position)
    assert position is not None and position.bearing is None


def test_dpmd_decodes_but_is_not_live_yet() -> None:
    (channel,) = [c for c in record.load_channels() if c.source == "dpmd"]
    assert (channel.source, channel.channel) in DECODERS
    assert channel.feeds == ("jdf",) and channel.live_feeds == ()
