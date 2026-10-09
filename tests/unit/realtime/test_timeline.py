"""Timeline scenarios: progress, events, off-route, predictions, lifecycle (ticket 5)."""

from __future__ import annotations

from datetime import date

from obehy.realtime.core import Context, estimate_all, step
from obehy.realtime.model import (
    Effect,
    FeedState,
    Instance,
    Interval,
    VehicleId,
    WriteEvent,
)
from obehy.realtime.policy import load_policy
from obehy.realtime.times import Instant
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
        estimate_all(self.state, self.ctx)
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


def near(value: Instant | None, clock: str, slack_s: int = 5) -> bool:
    """Within a few seconds: the test geometry is metres-approximate."""

    return (
        value is not None and abs((value - local(f"2026-10-08 {clock}")).total_seconds()) <= slack_s
    )


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


def test_minor_deviation_from_a_stop_to_stop_chord_counts_as_on_route() -> None:
    index = (
        timetable()
        .trip(
            "582492:143",
            days=[DAY],
            calls=[("A", "08:00"), ("B", "08:05"), ("C", "08:10")],
            shape=False,
        )
        .index()
    )
    run = Run()
    run.ctx = Context(index, POLICY)
    run.at("08:00", east(0)).at("08:02", north_of(300, 250))
    assert run.instance.off_route_since is None
    progress = run.instance.progress
    assert progress is not None and abs(progress.distance_m - 300) < 5


def test_source_delay_predicts_the_calls_ahead_without_gps() -> None:
    run = Run().at("08:01", None, delay=120)
    calls = run.instance.calls
    assert [c.status for c in calls] == ["predicted"] * 3
    assert calls[1].estimated_arrival == local("2026-10-08 08:07")
    assert calls[2].estimated_arrival == local("2026-10-08 08:12")
    assert {c.source_class for c in calls} == {"source"}


def test_gps_lateness_predicts_ahead_of_the_source_delay() -> None:
    run = Run().at("08:00", east(0)).at("08:04", east(500), delay=120)  # 90 s late at 500 m
    calls = run.instance.calls
    assert [c.status for c in calls] == ["actual", "predicted", "predicted"]
    assert near(calls[1].estimated_arrival, "08:06:30")
    assert near(calls[2].estimated_arrival, "08:11:30")
    assert calls[2].source_class == "gps"


def test_without_any_delay_the_measured_lateness_still_predicts() -> None:
    run = Run().at("08:00", east(0)).at("08:06", east(500))  # no source delay: 210 s late
    assert near(run.instance.calls[2].estimated_arrival, "08:13:30")


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
    assert near(run.instance.calls[2].estimated_arrival, "08:10:40")  # 40 s late at 1100 m


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
    estimate_all(run.state, run.ctx)
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
    expected = local("2026-10-08 08:05:30").timestamp()  # 30 s late at 500 m
    assert abs(update.stop_time_update[1].arrival.time - expected) <= 5
    assert feed_message(run.state, now).SerializeToString(deterministic=True) == raw

    # Stale after 300 s: the position goes, the predictions stay through a reception gap...
    later = local("2026-10-08 08:10")
    refresh(run.state, later, POLICY)
    assert [e.id for e in feed_message(run.state, later).entity] == [
        "trip:cis:line_trip:582492:143:2026-10-08"
    ]
    # ...until nothing has been heard for predict_without_data_s.
    lost = local("2026-10-08 08:34")
    refresh(run.state, lost, POLICY)
    assert len(feed_message(run.state, lost).entity) == 0


def test_duk_q11_only_the_lead_vehicle_drives_the_timeline() -> None:
    run = Run().at("08:00", east(0)).at("08:03", east(600))
    run.at("08:03:10", east(100), vehicle="1002")  # a second claimant far behind
    run.at("08:03:20", east(1800), vehicle="1002")  # ...or far ahead: neither moves progress
    progress = run.instance.progress
    assert progress is not None and abs(progress.distance_m - 600) < 5
    lead = run.instance.lead
    assert lead is not None and lead.vehicle == VehicleId("duk", "1001")
    assert run.state.vehicles[VehicleId("duk", "1002")].binding is not None
    run.at("08:08:30", east(1900), vehicle="1002")  # the lead went stale: 1002 takes over
    lead = run.instance.lead
    assert lead is not None and lead.vehicle == VehicleId("duk", "1002")


def test_close_stops_never_produce_events_out_of_order() -> None:
    index = (
        timetable()
        .stop("A", ORIGIN[0], LAT)
        .stop("B", east(1000)[0], LAT)
        .stop("C", east(1050)[0], LAT)  # 50 m after B: closer than both margins together
        .stop("D", east(2000)[0], LAT)
        .trip(
            "582492:143",
            days=[DAY],
            calls=[("A", "08:00"), ("B", "08:05"), ("C", "08:06"), ("D", "08:10")],
        )
        .index()
    )
    run = Run()
    run.ctx = Context(index, POLICY)
    run.at("08:00", east(0)).at("08:04", east(990)).at("08:05", east(1060)).at("08:09", east(2000))
    times = [
        t
        for c in run.instance.calls
        for t in (c.estimated_arrival, c.estimated_departure)
        if t is not None
    ]
    assert times == sorted(times)


def test_a_zavlek_is_driven_not_skipped() -> None:
    """A side branch out and back (A B C D C B E): a fix near B on the way in must not jump to
    B's second visit even when it lies closer to the way back out."""

    index = (
        timetable()
        .stop("A", ORIGIN[0], LAT)
        .stop("B", east(1000)[0], LAT)
        .stop("C", east(1000)[0], LAT + 900 / 111_195)
        .stop("D", east(1000)[0], LAT + 1800 / 111_195)
        .stop("E", east(1000)[0] + 0.012, LAT - 300 / 111_195)
        .trip(
            "582492:143",
            days=[DAY],
            calls=[
                ("A", "08:00"),
                ("B", "08:02"),
                ("C", "08:04"),
                ("D", "08:06"),
                ("C", "08:08"),
                ("B", "08:10"),
                ("E", "08:12"),
            ],
            shape=False,
        )
        .index()
    )
    run = Run()
    run.ctx = Context(index, POLICY)
    run.at("08:00", east(0)).at("08:01", east(500))
    # South-east of B, nearer the chord B->E (the way back out) than the chord A->B.
    run.at("08:01:30", (east(1000)[0] + 0.001, LAT - 150 / 111_195))
    run.at("08:03:30", (east(1000)[0], LAT + 850 / 111_195))
    run.at("08:05:30", (east(1000)[0], LAT + 1790 / 111_195))
    run.at("08:07:30", (east(1000)[0], LAT + 950 / 111_195))
    run.at("08:09:30", east(1000)).at("08:11:30", (east(1000)[0] + 0.012, LAT - 300 / 111_195))
    order = [
        (e.location_id[-1], e.visit_n, e.kind) for e in run.effects if isinstance(e, WriteEvent)
    ]
    arrivals = [(stop, visit) for stop, visit, kind in order if kind == "arrival"]
    assert arrivals == [("B", 1), ("C", 1), ("D", 1), ("C", 2), ("B", 2), ("E", 1)]


def _dwell_run() -> Run:
    """A-B-C with a real five-minute dwell at B (arrive 08:05, leave 08:10)."""

    index = (
        timetable()
        .trip(
            "582492:143",
            days=[DAY],
            calls=[("A", "08:00"), ("B", "08:05", "08:10"), ("C", "08:15")],
        )
        .index()
    )
    run = Run()
    run.ctx = Context(index, POLICY)
    return run


def test_a_late_bus_recovers_in_a_real_dwell() -> None:
    run = _dwell_run().at("08:00", east(0)).at("08:04", east(250))  # ~165 s late
    b, c = run.instance.calls[1:]
    assert near(b.estimated_arrival, "08:07:45")
    assert b.estimated_departure == local("2026-10-08 08:10")  # 2 min dwell fits: on time
    assert c.estimated_arrival == local("2026-10-08 08:15")


def test_a_dwell_absorbs_lateness_only_down_to_the_minimum_dwell() -> None:
    run = _dwell_run().at("08:00", east(0)).at("08:07:15", east(250))  # ~6 min late
    b, c = run.instance.calls[1:]
    assert near(b.estimated_arrival, "08:11")
    assert near(b.estimated_departure, "08:13")  # arrival + 2 min (long dwell)
    assert near(c.estimated_arrival, "08:18")


def test_early_running_is_carried_through_a_dwell_unchanged() -> None:
    run = _dwell_run().at("08:00", east(0)).at("08:00:45", east(500))  # ~105 s early
    b, c = run.instance.calls[1:]
    assert near(b.estimated_departure, "08:08:15")  # not clamped to the timetable
    assert near(c.estimated_arrival, "08:13:15")


def test_a_bus_standing_at_a_stop_gets_a_predicted_departure() -> None:
    run = _dwell_run().at("08:00", east(0)).at("08:06", east(500)).at("08:08", east(1000))
    run.at("08:08:30", east(1005))  # the arrival commits once the readings agree
    b = run.instance.calls[1]
    assert b.status == "actual" and b.estimated_arrival is not None
    assert b.estimated_departure == local("2026-10-08 08:10")  # waits for its time
