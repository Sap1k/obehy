"""Cancelled trips (core `_cancel`, BASE_PLAN.md section 20.7; PID-Q4): a source cancels a
journey that is not running; a vehicle seen starting it restores it."""

from __future__ import annotations

from datetime import date

from google.transit import gtfs_realtime_pb2 as rt

from obehy.realtime.core import Context, estimate_all, refresh, step
from obehy.realtime.emit.gtfs_rt import feed_message
from obehy.realtime.index import Index
from obehy.realtime.model import (
    Effect,
    FeedState,
    JourneyKey,
    ObservationResult,
    Reason,
    RecordCancellation,
    VehicleId,
)
from obehy.realtime.policy import load_policy
from tests.realtime.builder import ORIGIN, timetable, with_unknown
from tests.realtime.observations import local, observe

POLICY = load_policy()
D8 = date(2026, 10, 8)
NS = "cis:line_trip"
JOURNEY = JourneyKey("jdf", NS, "582492:143", D8)
CANCEL = {"vehicle": None, "source": "pid", "cancelled": True}


def _index() -> Index:
    tt = timetable().trip("582492:143", days=[D8], calls=[("A", "08:00"), ("B", "08:30")])
    return with_unknown(tt.index(), (NS, "582492:999"))


def run(*observations: dict[str, object]) -> tuple[FeedState, list[Effect]]:
    state = FeedState("jdf")
    ctx = Context(_index(), POLICY)
    effects: list[Effect] = []
    for item in observations:
        effects.extend(step(state, observe(**item), ctx))  # type: ignore[arg-type]
    return state, effects


def recorded(effects: list[Effect]) -> list[tuple[str, bool]]:
    return [(e.source, e.cancelled) for e in effects if isinstance(e, RecordCancellation)]


def test_pid_q4_a_cancellation_cancels_the_journey_and_every_call() -> None:
    state, effects = run({"at": "2026-10-08 08:00", **CANCEL})
    instance = state.instances[JOURNEY]
    assert instance.lifecycle == "cancelled"
    assert {c.status for c in instance.calls} == {"cancelled"}
    assert recorded(effects) == [("pid", True)]
    (result,) = [e for e in effects if isinstance(e, ObservationResult)]
    # No vehicle; read back on a warm start like a station board's row.
    assert (result.journey, result.state_since) == (JOURNEY, local("2026-10-08 08:00"))
    assert state.vehicles == {}
    # Neither estimated nor ever stale or lost: it does not run.
    later = local("2026-10-08 09:00")
    estimate_all(state, Context(_index(), POLICY), later)
    refresh(state, later, POLICY)
    instance = state.instances[JOURNEY]
    assert {c.status for c in instance.calls} == {"cancelled"}
    assert not (instance.freshness.stale or instance.freshness.lost)


def test_a_cancelled_trip_is_canceled_in_gtfs_rt() -> None:
    state, _ = run({"at": "2026-10-08 08:00", **CANCEL})
    message = feed_message(state, local("2026-10-08 08:10"), POLICY.lifecycle)
    (entity,) = message.entity
    update = entity.trip_update
    assert update.trip.schedule_relationship == rt.TripDescriptor.CANCELED
    assert (update.trip.start_date, len(update.stop_time_update)) == ("20261008", 0)
    assert not update.vehicle.id


def test_a_vehicle_seen_starting_it_restores_a_cancelled_journey() -> None:
    state, effects = run(
        {"at": "2026-10-08 07:58", **CANCEL},
        {"at": "2026-10-08 07:59", "state": "pre_trip", "position": ORIGIN},
        {"at": "2026-10-08 08:01", "state": "running", "position": ORIGIN},
    )
    instance = state.instances[JOURNEY]
    assert instance.lifecycle == "running"
    assert "cancelled" not in {c.status for c in instance.calls}
    assert recorded(effects) == [("pid", True), ("duk", False)]
    assert state.vehicles[VehicleId("duk", "1001")].status == "running"


def test_a_vehicle_waiting_for_a_cancelled_trip_does_not_restore_it() -> None:
    state, effects = run(
        {"at": "2026-10-08 07:58", **CANCEL},
        {"at": "2026-10-08 07:59", "state": "pre_trip", "position": ORIGIN},
    )
    assert state.instances[JOURNEY].lifecycle == "cancelled"
    assert recorded(effects) == [("pid", True)]
    assert state.vehicles[VehicleId("duk", "1001")].status == "positioning"


def test_a_journey_seen_running_is_not_cancelled() -> None:
    state, effects = run(
        {"at": "2026-10-08 08:01", "state": "running", "position": ORIGIN},
        {"at": "2026-10-08 08:02", **CANCEL},
    )
    assert state.instances[JOURNEY].lifecycle == "running"
    assert recorded(effects) == []


def test_a_cancellation_of_a_trip_not_running_that_day_is_unbound() -> None:
    state, effects = run({"at": "2026-10-08 08:00", **CANCEL, "key": "582492:999"})
    assert [(e.journey, e.reason) for e in effects if isinstance(e, ObservationResult)] == [
        (None, Reason.NO_TRIP)
    ]
    assert state.instances == {}
