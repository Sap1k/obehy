"""Progress scenarios (docs/R1_SLICE.md section 9): one test per route shape and data defect.

Geometry is in metres east/north of an origin at 50° N. Unless a test says otherwise the path is
the stop-to-stop chord (no shape), as for most JDF trips today.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from itertools import pairwise

from obehy.realtime.core import Context, estimate_all, step
from obehy.realtime.index import Index
from obehy.realtime.model import (
    Effect,
    FeedState,
    Instance,
    Observation,
    Position,
    RawRef,
    SourceState,
    TripKey,
    VehicleKey,
    WriteEvent,
)
from obehy.realtime.policy import load_policy
from obehy.realtime.times import PRAGUE, Instant, instant
from tests.realtime.builder import ORIGIN, CallSpec, timetable

POLICY = load_policy()
DAY = date(2026, 10, 8)
M_LON = 1 / 71_474.0  # degrees of longitude per metre at 50° N
M_LAT = 1 / 111_195.0

Fix = tuple[str, float, float] | tuple[str, float, float, float]  # clock, east, north[, bearing]


def at(clock: str) -> Instant:
    hours, minutes, *rest = clock.split(":")
    seconds = int(rest[0]) if rest else 0
    return instant(datetime(2026, 10, 8, int(hours), int(minutes), seconds, tzinfo=PRAGUE))


def point(east: float, north: float) -> tuple[float, float]:
    return ORIGIN[0] + east * M_LON, ORIGIN[1] + north * M_LAT


def route(stops: Mapping[str, tuple[float, float]], calls: Sequence[CallSpec]) -> Index:
    tt = timetable()
    for name, (east, north) in stops.items():
        lon, lat = point(east, north)
        tt.stop(name, lon, lat)
    return tt.trip("582492:143", days=[DAY], calls=list(calls), shape=False).index()


class Drive:
    def __init__(self, index: Index) -> None:
        self.ctx = Context(index, POLICY)
        self.state = FeedState("jdf")
        self.effects: list[Effect] = []

    def run(self, fixes: Sequence[Fix]) -> Drive:
        for fix in fixes:
            clock, east, north = fix[0], fix[1], fix[2]
            bearing = fix[3] if len(fix) == 4 else None
            lon, lat = point(east, north)
            when = at(clock)
            observation = Observation(
                "duk",
                "vehicles",
                "jdf",
                when,
                when,
                RawRef("0" * 64, 0),
                1,
                (
                    VehicleKey("1001"),
                    TripKey("cis:line_trip", "582492:143"),
                    Position(lat=lat, lon=lon, bearing=bearing),
                    SourceState("running"),
                ),
            )
            self.effects.extend(step(self.state, observation, self.ctx))
        return self

    @property
    def instance(self) -> Instance:
        estimate_all(self.state, self.ctx)
        (value,) = self.state.instances.values()
        return value

    def events(self) -> list[tuple[str, int, str]]:
        return [
            (e.location_id.split(":")[1], e.visit_n, e.kind)
            for e in self.effects
            if isinstance(e, WriteEvent)
        ]

    def times(self) -> list[Instant]:
        return [e.interval.hi for e in self.effects if isinstance(e, WriteEvent)]

    def arrivals(self) -> list[tuple[str, int]]:
        return [(stop, visit) for stop, visit, kind in self.events() if kind == "arrival"]


def drive(
    waypoints: Sequence[tuple[str, float, float]], every_s: int = 15, bearing: bool = False
) -> list[Fix]:
    """Fixes every `every_s` seconds along straight lines between timed waypoints."""

    fixes: list[Fix] = []
    for (c0, e0, n0), (c1, e1, n1) in pairwise(waypoints):
        t0, t1 = at(c0), at(c1)
        steps = max(1, int((t1 - t0).total_seconds() // every_s))
        heading = None
        if (e1, n1) != (e0, n0):
            heading = math.degrees(math.atan2(e1 - e0, n1 - n0)) % 360
        for k in range(steps):
            share = k / steps
            moment = t0 + (t1 - t0) * share
            clock = moment.astimezone(PRAGUE).strftime("%H:%M:%S")
            east, north = e0 + (e1 - e0) * share, n0 + (n1 - n0) * share
            if bearing and heading is not None:
                fixes.append((clock, east, north, heading))
            else:
                fixes.append((clock, east, north))
    last = waypoints[-1]
    fixes.append((last[0] if last[0].count(":") == 2 else last[0] + ":00", last[1], last[2]))
    return fixes


# --- route shapes --------------------------------------------------------------------------------

STRAIGHT = route(
    {"A": (0, 0), "B": (1000, 0), "C": (2000, 0)}, [("A", "08:00"), ("B", "08:02"), ("C", "08:04")]
)

# A závlek: B is left for the side branch to C and served again on the way back.
ZAVLEK_STOPS = {"A": (0, 0), "B": (1000, 0), "C": (1000, 800), "D": (2000, 0)}
ZAVLEK_CALLS = [("A", "08:00"), ("B", "08:02"), ("C", "08:04"), ("B", "08:06"), ("D", "08:08")]
ZAVLEK_DRIVE = [
    ("08:00", 0, 0),
    ("08:02", 1000, 0),
    ("08:04", 1000, 800),
    ("08:06", 1000, 0),
    ("08:08", 2000, 0),
]


def test_straight_run() -> None:
    d = Drive(STRAIGHT).run(drive([("08:00", 0, 0), ("08:02", 1000, 0), ("08:04", 2000, 0)]))
    assert d.events() == [
        ("A", 1, "departure"),
        ("B", 1, "arrival"),
        ("B", 1, "departure"),
        ("C", 1, "arrival"),
    ]
    assert d.instance.lifecycle == "finished"
    assert d.times() == sorted(d.times())


def test_zavlek_out_and_back_without_bearing() -> None:
    d = Drive(route(ZAVLEK_STOPS, ZAVLEK_CALLS)).run(drive(ZAVLEK_DRIVE))
    assert d.arrivals() == [("B", 1), ("C", 1), ("B", 2), ("D", 1)]
    assert d.times() == sorted(d.times())


def test_zavlek_out_and_back_with_bearing() -> None:
    d = Drive(route(ZAVLEK_STOPS, ZAVLEK_CALLS)).run(drive(ZAVLEK_DRIVE, bearing=True))
    assert d.arrivals() == [("B", 1), ("C", 1), ("B", 2), ("D", 1)]


def test_zavlek_with_no_reception_inside_it() -> None:
    fixes = [f for f in drive(ZAVLEK_DRIVE) if not at("08:02:30") < at(f[0]) < at("08:06:15")]
    d = Drive(route(ZAVLEK_STOPS, ZAVLEK_CALLS)).run(fixes)
    arrivals = d.arrivals()
    assert arrivals[0] == ("B", 1)
    assert arrivals[-1] == ("D", 1)
    assert ("B", 2) not in arrivals[:1]
    assert d.times() == sorted(d.times())  # nothing out of order, nothing guessed early


def test_stop_passed_on_the_way_out_and_served_on_the_way_back() -> None:
    index = route(
        {"A": (0, 0), "B": (1000, 0), "C": (2000, 0), "D": (2000, 800)},
        [("A", "08:00"), ("C", "08:04"), ("D", "08:06"), ("B", "08:09")],
    )
    d = Drive(index).run(
        drive([("08:00", 0, 0), ("08:04", 2000, 0), ("08:06", 2000, 800), ("08:09", 1000, 0)])
    )
    assert d.arrivals() == [("C", 1), ("D", 1), ("B", 1)]
    b_arrival = next(
        e for e in d.effects if isinstance(e, WriteEvent) and e.location_id.endswith("B")
    )
    assert b_arrival.interval.lo >= at("08:08")


def test_first_fix_inside_a_zavlek_is_resolved_by_the_next_fixes() -> None:
    fixes = [f for f in drive(ZAVLEK_DRIVE) if at(f[0]) >= at("08:05:00")]
    d = Drive(route(ZAVLEK_STOPS, ZAVLEK_CALLS)).run(fixes)
    assert d.arrivals() == [("B", 2), ("D", 1)]


def test_early_and_late_running_are_followed_not_rejected() -> None:
    early = Drive(STRAIGHT).run(drive([("07:50", 0, 0), ("07:52", 1000, 0), ("07:54", 2000, 0)]))
    assert early.arrivals() == [("B", 1), ("C", 1)]
    late = Drive(STRAIGHT).run(
        drive([("08:00", 0, 0), ("08:02", 1000, 0), ("08:17", 1000, 0), ("08:19", 2000, 0)])
    )
    assert late.arrivals() == [("B", 1), ("C", 1)]
    assert late.instance.lifecycle == "finished"


def test_a_route_through_its_own_start_is_not_read_as_far_ahead() -> None:
    # A Klášterec-style loop: the route comes back past its start (the stand is closer to the
    # second pass). Waiting at the start, the bus is at the origin, not 15 minutes ahead.
    loop = route(
        {"A": (0, 0), "B": (400, 0), "C": (400, 400), "A2": (0, 100), "D": (-400, 100)},
        [("A", "08:00"), ("B", "08:05"), ("C", "08:10"), ("A2", "08:15"), ("D", "08:20")],
    )
    d = Drive(loop).run([("08:00:00", 0, 100)])
    track = d.instance.track
    assert track is not None and track.hypotheses[0].along_m < 200
    assert track.hypotheses[0].lateness_s == 0


def test_duk_q19_a_vehicle_on_a_stand_past_the_first_stop_waits_for_its_time() -> None:
    # DPmÚL: "running" for 35 minutes on a stand 300 m past the first stop under the next trip.
    d = Drive(STRAIGHT).run(drive([("07:25", 300, 0), ("07:59:45", 300, 0)], every_s=60))
    assert d.instance.lifecycle == "pre_trip" and d.events() == []
    d.run(drive([("08:00", 300, 0), ("08:01", 1000, 0), ("08:03", 2000, 0)]))
    assert d.instance.lifecycle == "finished"
    assert min(d.times()) >= at("08:00")


def test_duk_q19_driving_to_the_start_over_the_trips_later_roads_is_not_running() -> None:
    # Half an hour before the start, moving along the end of the trip's path towards its start
    # (vehicle 809, 2026-10-06): not an early departure.
    d = Drive(STRAIGHT).run(drive([("07:30", 1500, 0), ("07:31", 1900, 0)]))
    assert d.instance.lifecycle == "pre_trip" and d.events() == []


def test_standing_at_a_stop_ahead_of_time_is_waiting_not_early_running() -> None:
    # Padded layover time: the bus reaches B four minutes before its arrival and waits.
    layover = route(
        {"A": (0, 0), "B": (1000, 0), "C": (2000, 0)},
        [("A", "08:00"), ("B", "08:10", "08:15"), ("C", "08:25")],
    )
    d = Drive(layover).run(drive([("08:00", 0, 0), ("08:06", 1000, 0), ("08:08", 1000, 0)]))
    c = d.instance.calls[2]
    assert c.estimated_arrival == at("08:25")


GAP_ROUTE = route(
    {"A": (0, 0), "B": (2000, 0), "C": (4000, 0), "D": (6000, 0)},
    [("A", "08:00"), ("B", "08:10"), ("C", "08:20"), ("D", "08:30")],
)


def test_a_long_reception_gap_gives_no_history_event_but_an_inferred_time() -> None:
    fixes = drive([("08:00", 0, 0), ("08:30", 6000, 0)])
    fixes = [f for f in fixes if not at("08:03") < at(f[0]) < at("08:24")]
    d = Drive(GAP_ROUTE).run(fixes)
    stops = {stop for stop, _, _ in d.events()}
    assert "B" not in stops and "C" not in stops  # inside the gap: not known well enough
    assert ("D", 1) in d.arrivals()
    b, c = d.instance.calls[1:3]
    assert (b.status, c.status) == ("inferred", "inferred")  # realtime still gets a time
    assert b.estimated_arrival is not None and c.estimated_arrival is not None
    assert at("08:03") < b.estimated_arrival < c.estimated_arrival < at("08:24")


def test_predictions_hold_the_last_lateness_through_a_reception_gap() -> None:
    # Five minutes late at 08:08, then nothing: the next stops stay five minutes late.
    fixes = drive([("08:00", 0, 0), ("08:05", 0, 0), ("08:13", 1600, 0)])
    d = Drive(GAP_ROUTE).run([f for f in fixes if at(f[0]) <= at("08:08")])
    b, c, dd = d.instance.calls[1:]
    assert (b.status, c.status, dd.status) == ("predicted",) * 3
    for call, planned in ((b, "08:15"), (c, "08:25"), (dd, "08:35")):
        assert call.estimated_arrival is not None
        assert abs((call.estimated_arrival - at(planned)).total_seconds()) <= 15


def test_jitter_around_a_stop_makes_no_backward_move_or_duplicate_event() -> None:
    fixes: list[Fix] = [("08:00:00", 0, 0), ("08:01:00", 500, 0)]
    for k, east in enumerate((990, 1020, 985, 1015, 995, 1010)):
        fixes.append((f"08:02:{10 * k:02d}", east, 0))
    fixes += [("08:03:00", 1500, 0), ("08:04:00", 2000, 0)]
    d = Drive(STRAIGHT).run(fixes)
    assert d.events().count(("B", 1, "arrival")) == 1
    assert d.events().count(("B", 1, "departure")) == 1


def test_a_real_detour_goes_off_route_then_resumes() -> None:
    fixes = drive([("08:00", 0, 0), ("08:01", 500, 0)])
    fixes += [(f"08:0{m}:00", 600, 5000) for m in range(2, 6)]
    fixes += drive([("08:06", 1200, 0), ("08:08", 2000, 0)])
    d = Drive(STRAIGHT)
    d.run(fixes[: len(fixes) - 9])
    assert d.instance.off_route
    d.run(fixes[len(fixes) - 9 :])
    assert not d.instance.off_route
    assert ("C", 1) in d.arrivals()


def test_a_chord_cutting_a_corner_still_matches() -> None:
    index = route({"A": (0, 0), "B": (3000, 0)}, [("A", "08:00"), ("B", "08:06")])
    d = Drive(index).run(drive([("08:00", 0, 0), ("08:03", 1500, 400), ("08:06", 3000, 0)]))
    assert d.arrivals() == [("B", 1)]
    assert not d.instance.off_route


def test_committed_progress_never_goes_back() -> None:
    d = Drive(route(ZAVLEK_STOPS, ZAVLEK_CALLS))
    last = -1.0
    for fix in drive(ZAVLEK_DRIVE):
        d.run([fix])
        track = d.instance.track
        assert track is not None
        assert track.committed_m >= last
        last = track.committed_m
