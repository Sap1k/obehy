"""The effect writer and the vehicle-day job against PostgreSQL (ticket 6)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, LiteralString

import psycopg
import pytest

from obehy.realtime.core import CORE_VERSION, Context, snapshot, step
from obehy.realtime.emit.db import Writer
from obehy.realtime.jobs import vehicle_day
from obehy.realtime.model import (
    AssignVehicle,
    Derivation,
    Effect,
    FeedState,
    JourneyKey,
    SnapshotJourney,
    VehicleId,
)
from obehy.realtime.policy import load_policy
from tests.realtime.builder import ORIGIN, STEP_LON, timetable
from tests.realtime.observations import local, observe

pytestmark = pytest.mark.postgres

POLICY = load_policy()
D8, D9 = date(2026, 10, 8), date(2026, 10, 9)
NS = "cis:line_trip"


def _scalar(connection: psycopg.Connection, query: LiteralString, *params: object) -> Any:
    row = connection.execute(query, params or None).fetchone()
    assert row is not None
    return row[0]


def _writer(connection: psycopg.Connection, release: str = "run-a") -> Writer:
    return Writer(connection, Derivation(CORE_VERSION, POLICY.version, release))


def _east(metres: float) -> tuple[float, float]:
    return (ORIGIN[0] + STEP_LON * metres / 1003.5, ORIGIN[1])


def _run() -> tuple[FeedState, list[Effect], Context]:
    index = (
        timetable()
        .trip("582492:143", days=[D8], calls=[("A", "08:00"), ("B", "08:05"), ("C", "08:10")])
        .index()
    )
    ctx = Context(index, POLICY)
    state = FeedState("jdf")
    effects: list[Effect] = []
    for clock, metres in (("08:00", 0), ("08:03", 500), ("08:06", 1200)):
        obs = observe(f"2026-10-08 {clock}", position=_east(metres), delay=60)
        effects.extend(step(state, obs, ctx))
    return state, effects, ctx


def test_effects_become_observations_history_and_state(connection: psycopg.Connection) -> None:
    state, effects, _ = _run()
    writer = _writer(connection)
    writer.write(effects)
    writer.write_state({"jdf": state})

    assert _scalar(connection, "SELECT count(*) FROM rt.observation") == 3
    assert _scalar(connection, "SELECT count(*) FROM rt.observation WHERE journey_key IS NULL") == 0
    assert _scalar(connection, "SELECT latest_revision FROM history.journey") == 1
    calls = connection.execute(
        "SELECT ordinal, location_id, visit_n, scheduled_departure, name"
        " FROM history.journey_call ORDER BY ordinal"
    ).fetchall()
    assert calls == [
        (1, "jdf:A", 1, 8 * 3600, "A"),
        (2, "jdf:B", 1, 8 * 3600 + 300, "B"),
        (3, "jdf:C", 1, 8 * 3600 + 600, "C"),
    ]
    events = connection.execute(
        "SELECT location_id, event_type, revision, interval_lo, interval_hi"
        " FROM history.actual_stop_event ORDER BY interval_lo, location_id, event_type"
    ).fetchall()
    assert [(e[0], e[1], e[2]) for e in events] == [
        ("jdf:A", "departure", 1),
        ("jdf:B", "arrival", 1),
        ("jdf:B", "departure", 1),
    ]
    assert events[0][3:] == (local("2026-10-08 08:00"), local("2026-10-08 08:03"))
    assigned = connection.execute(
        "SELECT source_vehicle_id, first_seen, last_seen FROM history.vehicle_assignment"
    ).fetchall()
    assert assigned == [("1001", local("2026-10-08 08:00"), local("2026-10-08 08:06"))]
    assert _scalar(connection, "SELECT status FROM rt.vehicle_state_current") == "running"
    assert _scalar(connection, "SELECT lifecycle FROM rt.trip_state_current") == "running"


def test_a_new_release_adds_a_revision_and_orphans_vanished_calls(
    connection: psycopg.Connection,
) -> None:
    _, effects, ctx = _run()
    _writer(connection).write(effects)
    journey = JourneyKey("jdf", NS, "582492:143", D8)
    first = snapshot(journey, ctx.index, "jdf:t1", local("2026-10-08 08:00"))
    without_b = replace(
        first,
        release_id="run-b",
        calls=tuple(c for c in first.calls if c.location_id != "jdf:B"),
    )
    second = _writer(connection, "run-b")
    second.write([without_b])
    second.write([without_b])  # the same release again is not a new revision

    assert _scalar(connection, "SELECT latest_revision FROM history.journey") == 2
    events = connection.execute(
        "SELECT location_id, event_type, revision, orphaned FROM history.actual_stop_event"
        " ORDER BY location_id, event_type"
    ).fetchall()
    assert events == [
        ("jdf:A", "departure", 2, False),
        ("jdf:B", "arrival", 1, True),
        ("jdf:B", "departure", 1, True),
    ]


def test_clear_history_removes_the_days_for_a_rebuild(connection: psycopg.Connection) -> None:
    _, effects, _ = _run()
    writer = _writer(connection)
    writer.write(effects)
    writer.clear_history([D8])
    assert _scalar(connection, "SELECT count(*) FROM history.journey") == 0
    assert _scalar(connection, "SELECT count(*) FROM history.actual_stop_event") == 0
    writer.write(effects)
    assert _scalar(connection, "SELECT count(*) FROM history.actual_stop_event") == 3


def _assign(writer: Writer, key: str, day: date, start: str, end: str) -> None:
    journey = JourneyKey("jdf", NS, key, day)
    vehicle = VehicleId("duk", "1001")
    writer.write(
        [
            SnapshotJourney(journey, local(start), "run-a", f"jdf:{key}", "582492", None, ()),
            AssignVehicle(vehicle, journey, local(start)),
            AssignVehicle(vehicle, journey, local(end)),
        ]
    )


def test_t13_vehicle_day_crosses_midnight_and_splits_at_long_gaps(
    connection: psycopg.Connection,
) -> None:
    writer = _writer(connection)
    _assign(writer, "582492:1", D8, "2026-10-08 22:00", "2026-10-08 23:30")
    _assign(writer, "582492:2", D9, "2026-10-09 00:15", "2026-10-09 01:00")
    _assign(writer, "582492:3", D9, "2026-10-09 06:00", "2026-10-09 07:00")

    assert vehicle_day(connection, D8, POLICY) == 1
    assert vehicle_day(connection, D9, POLICY) == 1
    rows = connection.execute(
        "SELECT service_date, seq, jsonb_array_length(journeys), tour_id"
        " FROM history.vehicle_day ORDER BY service_date, seq"
    ).fetchall()
    assert rows == [(D8, 1, 2, None), (D9, 1, 1, None)]
    assert vehicle_day(connection, D8, POLICY) == 1  # idempotent
