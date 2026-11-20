"""A vehicle seen at a stop: its events come from the fixes there (`Track.at_stop`).

The line is A (0 m), B (1000 m), C (2000 m), five minutes apart, no timetabled dwell.
"""

from __future__ import annotations

import random
from datetime import timedelta

from obehy.realtime.core import estimate_all
from obehy.realtime.model import Interval, WriteEvent
from tests.realtime.observations import local
from tests.unit.realtime.test_timeline import POLICY, Run, east, north_of


def at(clock: str) -> timedelta:
    """Seconds since 08:00:00, for stepping a clock."""

    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return timedelta(hours=hours - 8, minutes=minutes, seconds=seconds)


def clock(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    return f"{8 + total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def to_b() -> Run:
    """A bus on time into B."""

    return Run().at("08:00:00", east(0)).at("08:02:30", east(500)).at("08:04:40", east(900))


def event(run: Run, stop: str, kind: str) -> Interval:
    """The last event written for the stop: a later one replaces an earlier one in history."""

    found = [
        e.interval
        for e in run.effects
        if isinstance(e, WriteEvent) and e.location_id == f"jdf:{stop}" and e.kind == kind
    ]
    assert found, f"no {kind} at {stop}"
    return found[-1]


def test_a_bus_standing_in_gps_jitter_has_not_left() -> None:
    """Three minutes at B with fixes scattered up to 45 m along the road: forward jitter must
    not carry the reading over the departure trigger (40 m on)."""

    run = to_b()
    noise = random.Random(7)
    moment = at("08:05:00")
    while moment <= at("08:08:00"):
        run.at(clock(moment), east(1000 + noise.uniform(-45, 45)))
        assert run.instance.calls[1].passed_departure is None, clock(moment)
        moment += timedelta(seconds=10)
    run.at("08:08:10", east(1150)).at("08:08:20", east(1300))
    left = run.instance.calls[1].passed_departure
    assert left is not None
    assert local("2026-10-08 08:08:00") <= left <= local("2026-10-08 08:08:10")
    departure = event(run, "B", "departure")
    assert departure.lo >= local("2026-10-08 08:08:00")
    assert departure.hi <= local("2026-10-08 08:08:10")


def test_a_stop_reached_from_its_far_side_keeps_the_time_the_bus_stood_there() -> None:
    """A detour brings the bus onto the road beyond B; it pulls back to the stop, stands three
    minutes and drives on. B's departure is when it left, not when it came."""

    run = Run().at("08:00:00", east(0)).at("08:02:00", east(400))
    run.at("08:03:00", north_of(500, 300)).at("08:04:00", north_of(900, 300))
    run.at("08:05:00", north_of(1150, 300))
    run.at("08:05:30", east(1090))  # onto the road, 90 m past the stop
    run.at("08:05:45", east(1010))  # back at the stop
    for seconds in range(60, 240, 15):
        run.at(clock(at("08:05:00") + timedelta(seconds=seconds)), east(1005))
    run.at("08:09:10", east(1200)).at("08:09:40", east(1500))
    b = run.instance.calls[1]
    assert b.passed_arrival is not None and b.passed_departure is not None
    assert b.passed_arrival <= local("2026-10-08 08:05:45")
    assert local("2026-10-08 08:08:45") <= b.passed_departure <= local("2026-10-08 08:09:10")
    departure = event(run, "B", "departure")
    assert departure.lo >= local("2026-10-08 08:08:45")
    assert departure.hi <= local("2026-10-08 08:09:10")
    assert event(run, "B", "arrival").hi <= local("2026-10-08 08:05:45")


def test_a_bus_seen_leaving_keeps_its_shown_departure_until_it_is_recorded() -> None:
    """Between the first fix away and the recorded departure, time passing moves nothing."""

    run = to_b().at("08:05:00", east(1000)).at("08:06:30", east(1000))
    run.at("08:06:40", east(1100))
    shown = run.instance.calls[1].estimated_departure
    assert shown is not None
    assert local("2026-10-08 08:06:30") <= shown <= local("2026-10-08 08:06:40")
    for later in ("08:06:50", "08:07:20", "08:08:00"):
        estimate_all(run.state, run.ctx, local(f"2026-10-08 {later}"))
        (instance,) = run.state.instances.values()
        assert instance.calls[1].estimated_departure == shown, later
    run.at("08:08:05", east(1600))
    assert run.instance.calls[1].estimated_departure == shown


def test_a_bus_driving_through_a_stop_is_timed_between_its_fixes() -> None:
    """Never seen at B: both events fall between the fixes either side, as before."""

    run = to_b().at("08:04:55", east(930)).at("08:05:15", east(1100))
    b = run.instance.calls[1]
    assert b.passed_arrival is not None and b.passed_departure is not None
    assert local("2026-10-08 08:04:55") <= b.passed_arrival <= b.passed_departure
    assert b.passed_departure <= local("2026-10-08 08:05:15")
    assert POLICY.lifecycle.departure_margin_m == 40
