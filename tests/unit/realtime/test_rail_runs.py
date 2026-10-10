"""Rail runs (docs/R2_SLICE.md section 2, scenarios R1-R5): one instance per CZPTT path, public
journeys per train number, output per published trip part."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from obehy.realtime.core import Context, estimate_all, step
from obehy.realtime.emit.gtfs_rt import feed_message
from obehy.realtime.index import Index
from obehy.realtime.model import (
    AssignVehicle,
    Effect,
    FeedState,
    JourneyKey,
    LinkJourneys,
    ObservationResult,
    Reason,
    SnapshotJourney,
    WriteEvent,
)
from obehy.realtime.policy import load_policy
from tests.realtime.builder import ORIGIN, STEP_LON, timetable
from tests.realtime.observations import local, observe

POLICY = load_policy()
D8 = date(2026, 10, 8)
TRAIN = "czptt:train_number"
PA = JourneyKey("czptt", "czptt:pa", "PA1", D8)
J1 = JourneyKey("czptt", TRAIN, "106006", D8)
J2 = JourneyKey("czptt", TRAIN, "6006", D8)


def _run(*, number_change: bool = True, days: tuple[date, ...] = (D8,)) -> Index:
    """A ~ X B | B C: part 1 (A..B, X a railway point) and part 2 (B..C), sharing B."""

    tt = timetable("czptt", TRAIN)
    tt.trip(
        "106006",
        trip_id="czptt:trip:PA1:1",
        days=days,
        calls=[("A", "08:00"), ("~X", "08:05"), ("B", "08:10", "08:10")],
        run=("PA1", 1),
        train="106006",
        mode="rail",
        shape=False,
    )
    tt.trip(
        "6006" if number_change else "106006",
        trip_id="czptt:trip:PA1:2",
        days=days,
        calls=[("B", "08:10", "08:12"), ("C", "08:20")],
        run=("PA1", 2),
        train="6006" if number_change else "106006",
        mode="rail",
        shape=False,
        first_sequence=3,
    )
    tt.key("czptt:tr", "Tr:1", "czptt:trip:PA1:1", "czptt:trip:PA1:2")
    return tt.index()


def _step(index: Index, *observations: Mapping[str, object]) -> tuple[FeedState, list[Effect]]:
    state = FeedState("czptt")
    ctx = Context(index, POLICY)
    effects: list[Effect] = []
    for item in observations:
        values = {"feed": "czptt", "namespace": TRAIN, **item}
        effects.extend(step(state, observe(**values), ctx))  # type: ignore[arg-type]
    return state, effects


def _at(n: float) -> tuple[float, float]:
    return ORIGIN[0] + n * STEP_LON, ORIGIN[1]


def test_r1_r2_a_run_is_one_instance_with_a_journey_per_train_number() -> None:
    state, effects = _step(_run(), {"at": "2026-10-08 08:00", "key": "6006"})
    assert list(state.instances) == [PA]
    instance = state.instances[PA]
    # X is a railway point; B is shared by both parts and both journeys.
    assert [c.passenger for c in instance.calls] == [True, False, True, True]
    assert [(p.first, p.last) for p in instance.parts] == [(0, 2), (2, 3)]
    assert [(j.journey, j.first, j.last) for j in instance.journeys] == [(J1, 0, 2), (J2, 2, 3)]
    snapshots = [e for e in effects if isinstance(e, SnapshotJourney)]
    assert [(s.journey, s.trip_id, s.run_key, len(s.calls)) for s in snapshots] == [
        (J1, "czptt:trip:PA1:1", "PA1", 3),
        (J2, "czptt:trip:PA1:2", "PA1", 2),
    ]
    assert [e for e in effects if isinstance(e, LinkJourneys)] == [
        LinkJourneys(J1, J2, "continues_as")
    ]
    assert {e.journey for e in effects if isinstance(e, AssignVehicle)} == {J1, J2}
    (result,) = [e for e in effects if isinstance(e, ObservationResult)]
    assert (result.journey, result.public) == (PA, (J1, J2))


def test_r1_one_train_number_is_one_journey() -> None:
    state, _ = _step(_run(number_change=False), {"at": "2026-10-08 08:00", "key": "106006"})
    assert [j.journey for j in state.instances[PA].journeys] == [J1]


def test_r5_a_train_number_and_a_tr_bind_the_same_run() -> None:
    state, _ = _step(
        _run(),
        {"at": "2026-10-08 08:00", "key": "106006", "vehicle": "dúk", "source": "duk"},
        {"at": "2026-10-08 08:01", "key": "Tr:1", "namespace": "czptt:tr", "source": "sz-mapa"},
    )
    assert list(state.instances) == [PA]
    assert {v.binding.journey for v in state.vehicles.values() if v.binding} == {PA}


def test_a_key_of_a_run_not_running_that_day_is_not_active() -> None:
    _, effects = _step(_run(days=(date(2026, 10, 9),)), {"at": "2026-10-07 12:00", "key": "6006"})
    assert [e.reason for e in effects if isinstance(e, ObservationResult)] == [Reason.NOT_ACTIVE]


def test_events_go_to_the_public_journey_and_the_junction_splits() -> None:
    fixes = [
        ("2026-10-08 07:59:30", 0.0),
        ("2026-10-08 08:00:30", 0.2),
        ("2026-10-08 08:09:00", 1.9),
        ("2026-10-08 08:10:00", 2.0),
        ("2026-10-08 08:12:30", 2.2),
        ("2026-10-08 08:16:00", 2.6),
        ("2026-10-08 08:20:00", 3.0),
    ]
    _, effects = _step(
        _run(),
        *({"at": at, "key": "6006", "state": "running", "position": _at(x)} for at, x in fixes),
    )
    events = {
        (e.journey.key, e.location_id, e.visit_n, e.kind)
        for e in effects
        if isinstance(e, WriteEvent)
    }
    assert ("106006", "czptt:A", 1, "departure") in events
    assert ("106006", "czptt:B", 1, "arrival") in events
    assert ("6006", "czptt:B", 1, "departure") in events
    assert ("6006", "czptt:C", 1, "arrival") in events


def test_gtfs_rt_has_a_trip_update_per_part_without_railway_points() -> None:
    index = _run()
    state, _ = _step(
        index,
        {"at": "2026-10-08 08:00:30", "key": "6006", "state": "running", "position": _at(0.2)},
        {"at": "2026-10-08 08:02:00", "key": "6006", "state": "running", "position": _at(0.5)},
    )
    estimate_all(state, Context(index, POLICY))
    message = feed_message(state, local("2026-10-08 08:02"))
    updates = {
        e.trip_update.trip.trip_id: e.trip_update
        for e in message.entity
        if e.HasField("trip_update")
    }
    assert sorted(updates) == ["czptt:trip:PA1:1", "czptt:trip:PA1:2"]
    first = updates["czptt:trip:PA1:1"].stop_time_update
    second = updates["czptt:trip:PA1:2"].stop_time_update
    assert [u.stop_sequence for u in first] == [3]  # A passed unobserved; X never published
    assert first[0].HasField("arrival") and not first[0].HasField("departure")
    assert [u.stop_sequence for u in second] == [3, 4]
    assert second[0].HasField("departure") and not second[0].HasField("arrival")
    (vehicle,) = [e.vehicle for e in message.entity if e.HasField("vehicle")]
    assert vehicle.trip.trip_id == "czptt:trip:PA1:1"
