"""Timeline scenarios: progress, events, off-route, predictions, lifecycle (ticket 5)."""

from __future__ import annotations

from datetime import date

from obehy.realtime.core import Context, step
from obehy.realtime.model import (
    Effect,
    FeedState,
    Instance,
    Interval,
    VehicleId,
    WriteEvent,
)
from obehy.realtime.policy import load_policy
from tests.realtime.builder import ORIGIN, STEP_LON, timetable, with_unknown
from tests.realtime.observations import local, observe

POLICY = load_policy()
DAY = date(2026, 10, 8)
LAT = ORIGIN[1]


def east(metres: float) -> tuple[float, float]:
    """A point on the A-B-C line, `metres` east of A."""

    return (ORIGIN[0] + STEP_LON * metres / 1000 * (1000 / 1003.5), LAT)


def north_of(metres_east: float, metres_north: float) -> tuple[float, float]:
    return (east(metres_east)[0], LAT + metres_north / 111_195)


class Run:
    def __init__(self) -> None:
        index = (
            timetable()
            .trip(
                "582492:143",
                days=[DAY],
                calls=[("A", "08:00"), ("B", "08:05"), ("C", "08:10")],
            )
            .index()
        )
        self.ctx = Context(with_unknown(index, ("cis:line_trip", "999999:1")), POLICY)
        self.state = FeedState("jdf")
        self.effects: list[Effect] = []

    def at(self, clock: str, where: tuple[float, float] | None, **extra: object) -> Run:
        obs = observe(f"2026-10-08 {clock}", position=where, **extra)  # type: ignore[arg-type]
        self.effects.extend(step(self.state, obs, self.ctx))
        return self

    @property
    def instance(self) -> Instance:
        (value,) = self.state.instances.values()
        return value

    def events(self) -> list[tuple[str, str, Interval]]:
        return [
            (e.location_id.split(":")[1], e.kind, e.interval)
            for e in self.effects
            if isinstance(e, WriteEvent)
        ]


def between(a: str, b: str) -> Interval:
    return Interval(local(f"2026-10-08 {a}"), local(f"2026-10-08 {b}"))


def test_events_follow_progress_and_the_trip_finishes() -> None:
    run = (
        Run()
        .at("08:00", east(0))
        .at("08:03", east(500))
        .at("08:06", east(1200))
        .at("08:11", east(2000))
    )
    assert run.events() == [
        ("A", "departure", between("08:00", "08:03")),
        ("B", "arrival", between("08:03", "08:06")),
        ("B", "departure", between("08:03", "08:06")),
        ("C", "arrival", between("08:06", "08:11")),
    ]
    assert run.instance.lifecycle == "finished"
    assert [c.status for c in run.instance.calls] == ["actual"] * 3
    assert run.state.vehicles[VehicleId("duk", "1001")].status == "layover"


def test_progress_is_monotone_and_ignores_backward_fixes() -> None:
    run = Run().at("08:00", east(0)).at("08:03", east(600))
    run.at("08:03", east(560))  # jitter within the backtrack tolerance
    progress = run.instance.progress
    assert progress is not None and abs(progress.distance_m - 600) < 5
    run.at("08:04", east(100))  # far behind: ignored, never rewinds
    progress = run.instance.progress
    assert progress is not None and abs(progress.distance_m - 600) < 5
    assert [kind for _, kind, _ in run.events()] == ["departure"]


def test_off_route_holds_progress_then_flags_then_rejoins() -> None:
    run = Run().at("08:00", east(0)).at("08:02", east(300))
    run.at("08:03", north_of(400, 1000))
    assert run.instance.off_route_since is not None and not run.instance.off_route
    run.at("08:06:30", north_of(500, 1000))
    assert run.instance.off_route
    progress = run.instance.progress
    assert progress is not None and abs(progress.distance_m - 300) < 5
    run.at("08:07", east(900))
    assert not run.instance.off_route and run.instance.off_route_since is None
    assert run.state.vehicles[VehicleId("duk", "1001")].binding is not None


def test_minor_deviation_inside_tolerance_counts_as_on_route() -> None:
    run = Run().at("08:00", east(0)).at("08:02", north_of(300, 250))
    assert run.instance.off_route_since is None
    progress = run.instance.progress
    assert progress is not None and abs(progress.distance_m - 300) < 5


def test_source_delay_predicts_the_calls_ahead() -> None:
    run = Run().at("08:01", east(0), delay=120)
    calls = run.instance.calls
    assert [c.status for c in calls] == ["predicted"] * 3
    assert calls[1].estimated_arrival == local("2026-10-08 08:07")
    assert calls[2].estimated_arrival == local("2026-10-08 08:12")
    assert {c.source_class for c in calls} == {"source"}


def test_duk_q6_garbage_negative_delay_is_discarded() -> None:
    run = Run().at("08:01", east(0), delay=120).at("08:02", east(100), delay=-3600)
    assert run.instance.delay_s == 120


def test_predictions_never_precede_the_last_fix() -> None:
    run = Run().at("08:00", east(0)).at("08:09", east(1100), delay=0)
    c = run.instance.calls[2]
    assert c.estimated_arrival is not None
    assert c.estimated_arrival >= local("2026-10-08 08:09")


def test_duk_q5_pre_trip_vehicle_makes_no_progress_or_events() -> None:
    run = (
        Run()
        .at("07:50", east(0), state="pre_trip")
        .at("07:55", east(1200), state="pre_trip")
        .at("07:58", east(2000), state="pre_trip")
    )
    assert run.events() == []
    assert run.instance.progress is None and run.instance.lifecycle == "pre_trip"


def test_binding_mid_trip_marks_passed_calls_without_realtime() -> None:
    run = Run().at("08:06", east(1100), delay=60)
    statuses = [c.status for c in run.instance.calls]
    assert statuses == ["no_realtime", "no_realtime", "predicted"]
    assert run.events() == []
    assert run.instance.calls[2].estimated_arrival == local("2026-10-08 08:11")


def test_a_loop_attaches_events_to_the_right_visit() -> None:
    index = (
        timetable()
        .trip(
            "582492:143",
            days=[DAY],
            calls=[("A", "08:00"), ("B", "08:05"), ("A", "08:10")],
            shape=False,
        )
        .index()
    )
    run = Run()
    run.ctx = Context(index, POLICY)
    run.at("08:00", east(0)).at("08:03", east(500)).at("08:06", east(1000))
    run.at("08:08", east(500)).at("08:11", east(0))
    visits = [(e.location_id, e.visit_n, e.kind) for e in run.effects if isinstance(e, WriteEvent)]
    assert visits == [
        ("jdf:A", 1, "departure"),
        ("jdf:B", 1, "arrival"),
        ("jdf:B", 1, "departure"),
        ("jdf:A", 2, "arrival"),
    ]
    assert run.instance.lifecycle == "finished"


def test_gtfs_rt_carries_matched_journeys_and_vehicles_only() -> None:
    from google.transit import gtfs_realtime_pb2 as rt

    from obehy.realtime.core import refresh
    from obehy.realtime.emit.gtfs_rt import feed_message

    run = Run().at("08:00", east(0)).at("08:03", east(500), delay=60)
    run.at("08:03", None, vehicle="2002", key="999999:1")  # unmatched: never in GTFS-RT
    now = local("2026-10-08 08:03:10")
    refresh(run.state, now, POLICY)
    raw = feed_message(run.state, now).SerializeToString(deterministic=True)
    message = rt.FeedMessage()
    message.ParseFromString(raw)

    assert [e.id for e in message.entity] == [
        "trip:cis:line_trip:582492:143:2026-10-08",
        "vehicle:duk:1001",
    ]
    update = message.entity[0].trip_update
    assert (update.trip.trip_id, update.trip.start_date) == ("jdf:t1", "20261008")
    assert [s.stop_sequence for s in update.stop_time_update] == [1, 2, 3]
    assert update.stop_time_update[1].arrival.time == int(local("2026-10-08 08:06").timestamp())
    assert feed_message(run.state, now).SerializeToString(deterministic=True) == raw

    refresh(run.state, local("2026-10-08 08:10"), POLICY)  # stale after 180 s
    assert len(feed_message(run.state, local("2026-10-08 08:10")).entity) == 0
