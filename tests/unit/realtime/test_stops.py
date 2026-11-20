"""A vehicle seen at a stop: its events come from the fixes there (`Track.at_stop`).

The line is A (0 m), B (1000 m), C (2000 m), five minutes apart, no timetabled dwell.
"""

from __future__ import annotations

import random
from datetime import timedelta

from obehy.realtime.core import estimate_all
from obehy.realtime.facts import Interval
from obehy.realtime.model import WriteEvent
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


def _close_stops(minutes: tuple[str, str, str, str]) -> Run:
    """A (0 m), B (1000 m), C (1300 m), D (2300 m) at the given times."""

    from obehy.realtime.core import Context
    from tests.realtime.builder import ORIGIN, timetable
    from tests.unit.realtime.test_timeline import DAY, LAT

    index = (
        timetable()
        .stop("A", ORIGIN[0], LAT)
        .stop("B", east(1000)[0], LAT)
        .stop("C", east(1300)[0], LAT)
        .stop("D", east(2300)[0], LAT)
        .trip(
            "582492:143",
            days=[DAY],
            calls=list(zip(("A", "B", "C", "D"), minutes, strict=True)),
        )
        .index()
    )
    run = Run()
    run.ctx = Context(index, POLICY)
    return run


def _departures(run: Run, now: str) -> list[timedelta]:
    estimate_all(run.state, run.ctx, local(f"2026-10-08 {now}"))
    (instance,) = run.state.instances.values()
    start = local("2026-10-08 08:00:00")
    return [c.estimated_departure - start for c in instance.calls[1:3] if c.estimated_departure]


def test_stops_in_one_timetable_minute_never_leave_together() -> None:
    """B and C both at 08:05, 300 m apart; the bus stands late at B. C is not reached before B
    is left, and is left a dwell after it is reached: never at B's departure time."""

    run = _close_stops(("08:00", "08:05", "08:05", "08:10"))
    run.at("08:00:00", east(0)).at("08:03:00", east(600)).at("08:06:00", east(1000))
    run.at("08:07:00", east(1000))
    b, c = _departures(run, "08:07:10")
    (instance,) = run.state.instances.values()
    c_arrival = instance.calls[2].estimated_arrival
    assert c_arrival is not None
    dwell = timedelta(seconds=POLICY.prediction.dwell_credit_s("bus"))
    assert c_arrival - local("2026-10-08 08:00:00") >= b
    assert c >= b + dwell


def test_a_predicted_departure_ahead_is_a_typical_dwell_after_its_arrival() -> None:
    run = _close_stops(("08:00", "08:05", "08:07", "08:10"))
    run.at("08:00:00", east(0)).at("08:02:00", east(400))
    estimate_all(run.state, run.ctx, local("2026-10-08 08:02:05"))
    (instance,) = run.state.instances.values()
    dwell = timedelta(seconds=POLICY.prediction.dwell_credit_s("bus"))
    for call in instance.calls[1:3]:
        assert call.estimated_arrival is not None and call.estimated_departure is not None
        assert call.estimated_departure - call.estimated_arrival == dwell


def test_a_bus_waiting_on_a_stand_off_the_path_leaves_its_first_stop_when_it_drives_off() -> None:
    """The stand lies 250 m from where the timetable puts A, off the path: the departure is
    when the bus stopped standing there, not when it was first placed on the path."""

    run = Run().at("07:58:00", north_of(0, 250)).at("07:59:00", north_of(0, 250))
    run.at("08:01:30", north_of(0, 251)).at("08:02:00", east(350)).at("08:03:00", east(700))
    run.at("08:05:00", east(1000)).at("08:05:20", east(1100))
    a = run.instance.calls[0]
    assert a.passed_departure is not None
    assert local("2026-10-08 08:01:30") <= a.passed_departure <= local("2026-10-08 08:02:00")


def test_a_bus_parked_short_of_its_terminus_arrived_when_it_stopped() -> None:
    """C's stand is 300 m short of where the timetable puts it; the bus parks there, keeps
    reporting, then goes quiet: it arrived when it stopped."""

    from obehy.realtime.core import refresh

    run = to_b().at("08:05:00", east(1000)).at("08:05:30", east(1150)).at("08:08:00", east(1600))
    run.at("08:09:00", east(1700))
    for minute in range(10, 16):
        run.at(f"08:{minute:02d}:00", east(1701))
    for later in ("08:25:00", "08:30:00"):
        now = local(f"2026-10-08 {later}")
        run.effects.extend(refresh(run.state, now, run.ctx))
    (instance,) = run.state.instances.values()
    assert instance.lifecycle == "finished"
    assert instance.calls[2].passed_arrival == local("2026-10-08 08:09:00")
    assert event(run, "C", "arrival") == Interval(
        local("2026-10-08 08:09:00"), local("2026-10-08 08:09:00")
    )


def test_a_bus_lost_just_short_of_its_terminus_after_a_long_stop_still_arrives() -> None:
    """Five minutes at B, then on time over the last leg and lost 150 m short of C: its arrival
    is inferred from the lateness on that leg, the dwell at B notwithstanding."""

    from obehy.realtime.core import refresh

    run = to_b().at("08:05:00", east(1000)).at("08:07:00", east(1000)).at("08:10:00", east(1000))
    run.at("08:10:20", east(1150)).at("08:12:00", east(1500)).at("08:14:00", east(1850))
    for later in ("08:20:00", "08:25:00"):
        run.effects.extend(refresh(run.state, local(f"2026-10-08 {later}"), run.ctx))
    (instance,) = run.state.instances.values()
    assert instance.lifecycle == "finished"
    arrival = event(run, "C", "arrival")
    assert local("2026-10-08 08:14:00") <= arrival.lo <= local("2026-10-08 08:16:00")
