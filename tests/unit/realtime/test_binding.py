"""Keyed binding scenarios T2-T5 and the DÚK quirks handled by the core."""

from __future__ import annotations

from datetime import date

from obehy.realtime.core import Context, step
from obehy.realtime.index import Index
from obehy.realtime.model import (
    AssignVehicle,
    Effect,
    FeedState,
    JourneyKey,
    ObservationResult,
    Reason,
    SnapshotJourney,
    VehicleId,
)
from obehy.realtime.policy import load_policy
from tests.realtime.builder import days_between, timetable, with_unknown
from tests.realtime.observations import observe

POLICY = load_policy()
D8, D9 = date(2026, 10, 8), date(2026, 10, 9)
NS = "cis:line_trip"


def run(index: Index, *observations: str | dict[str, object]) -> tuple[FeedState, list[Effect]]:
    state = FeedState("jdf")
    effects: list[Effect] = []
    ctx = Context(index, POLICY)
    for item in observations:
        obs = observe(item) if isinstance(item, str) else observe(**item)  # type: ignore[arg-type]
        effects.extend(step(state, obs, ctx))
    return state, effects


def results(effects: list[Effect]) -> list[tuple[JourneyKey | None, Reason | None]]:
    return [(e.journey, e.reason) for e in effects if isinstance(e, ObservationResult)]


def vehicle(state: FeedState, number: str = "1001") -> object:
    return state.vehicles[VehicleId("duk", number)]


def test_t2_night_trip_belongs_to_yesterdays_service_date() -> None:
    index = timetable().trip("582492:143", days=[D8], calls=[("A", "23:50"), ("B", "24:30")])
    state, effects = run(index.index(), "2026-10-09 00:20")
    assert results(effects) == [(JourneyKey("jdf", NS, "582492:143", D8), None)]
    assert state.vehicles[VehicleId("duk", "1001")].status == "running"


def test_t3_vehicle_waiting_for_tomorrows_first_trip() -> None:
    index = timetable().trip("582492:143", days=[D9], calls=[("A", "00:05"), ("B", "00:30")])
    state, effects = run(index.index(), {"at": "2026-10-08 23:55", "state": "pre_trip"})
    journey = JourneyKey("jdf", NS, "582492:143", D9)
    assert results(effects) == [(journey, None)]
    assert state.instances[journey].lifecycle == "pre_trip"
    assert state.vehicles[VehicleId("duk", "1001")].status == "positioning"


def test_t4_rollover_keeps_the_running_trips_service_date() -> None:
    days = days_between(D8, date(2026, 10, 10))
    index = timetable().trip("582492:143", days=days, calls=[("A", "22:00"), ("B", "23:00")])
    _, effects = run(
        index.index(),
        {"at": "2026-10-08 23:58", "delay": 5400},
        {"at": "2026-10-09 00:25", "delay": 5400},
    )
    journey = JourneyKey("jdf", NS, "582492:143", D8)
    assert results(effects) == [(journey, None), (journey, None)]


def test_t5_duk_q4_yesterdays_key_in_the_morning_is_not_in_service() -> None:
    index = timetable().trip("582492:143", days=[D8], calls=[("A", "22:00"), ("B", "22:40")])
    state, effects = run(index.index(), "2026-10-08 22:10", "2026-10-09 06:00")
    journey = JourneyKey("jdf", NS, "582492:143", D8)
    assert results(effects) == [(journey, None), (None, Reason.NOT_IN_SERVICE)]
    current = state.vehicles[VehicleId("duk", "1001")]
    assert (current.status, current.binding) == ("not_in_service", None)
    assert state.instances[journey].freshness.updated_at < current.last_seen  # not extended


def test_duk_q3_off_calendar_key_is_taken_literally() -> None:
    weekend = date(2026, 10, 10)
    index = timetable().trip("300001:301", days=[weekend], calls=[("A", "08:00"), ("B", "08:30")])
    state, effects = run(index.index(), {"at": "2026-10-08 08:05", "key": "300001:301"})
    assert results(effects) == [(None, Reason.NOT_ACTIVE)]
    assert state.vehicles[VehicleId("duk", "1001")].status == "unmatched"


def test_unknown_line_and_unknown_trip_are_told_apart() -> None:
    index = timetable().trip("582492:143", days=[D8], calls=[("A", "08:00"), ("B", "08:30")])
    loaded = with_unknown(index.index(), (NS, "582492:999"), (NS, "626:1"))
    _, effects = run(
        loaded,
        {"at": "2026-10-08 08:05", "key": "582492:999"},
        {"at": "2026-10-08 08:05", "key": "626:1", "vehicle": "1002"},
    )
    assert results(effects) == [(None, Reason.NO_TRIP), (None, Reason.NO_LINE)]


def test_duk_q11_two_vehicles_share_one_journey() -> None:
    index = timetable().trip("582492:143", days=[D8], calls=[("A", "08:00"), ("B", "08:30")])
    state, effects = run(
        index.index(),
        {"at": "2026-10-08 08:05", "vehicle": "1001"},
        {"at": "2026-10-08 08:05", "vehicle": "1002"},
    )
    assert len(state.instances) == 1
    assert sum(isinstance(e, SnapshotJourney) for e in effects) == 1
    assert sum(isinstance(e, AssignVehicle) for e in effects) == 2


def test_observation_without_a_key_is_unmatched() -> None:
    state, effects = run(timetable().index(), {"at": "2026-10-08 08:05", "key": None})
    assert results(effects) == [(None, Reason.NO_KEY)]
    assert state.vehicles[VehicleId("duk", "1001")].status == "unmatched"


def test_a_running_source_state_starts_the_trip() -> None:
    index = timetable().trip("582492:143", days=[D8], calls=[("A", "08:00"), ("B", "08:30")])
    state, _ = run(
        index.index(),
        {"at": "2026-10-08 07:50", "state": "pre_trip"},
        {"at": "2026-10-08 08:01", "state": "running"},
    )
    (instance,) = state.instances.values()
    assert instance.lifecycle == "running"
