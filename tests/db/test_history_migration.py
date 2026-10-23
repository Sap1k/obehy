"""Migration 0020: history recorded in the old wide, monthly tables moves to the slim daily ones."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import conninfo, sql

from obehy.release.migrate import MIGRATIONS, migrate

TABLES = (
    "journey",
    "journey_schedule",
    "journey_call",
    "actual_stop_event",
    "vehicle_assignment",
    "vehicle_day",
    "journey_link",
    "platform_evidence",
    "journey_cancellation",
)

CALLS = [
    ("jdf", "ns", "k1", "2026-10-08", 1, 1, "jdf:A", 1, True, None, 28800, "A"),
    ("jdf", "ns", "k1", "2026-10-08", 1, 2, "jdf:B", 1, True, 29100, None, "B"),
    ("jdf", "ns", "k2", "2026-10-09", 1, 1, "jdf:A", 1, True, None, 30000, "A"),
]
EVENTS = [
    ("jdf", "ns", "k1", "2026-10-08", "jdf:A", 1, "departure", 1, "08:00", "gps", "duk", False),
    ("jdf", "ns", "k1", "2026-10-08", "jdf:B", 1, "arrival", 1, "08:05", "board", "pid", True),
    ("jdf", "ns", "k2", "2026-10-09", "jdf:A", 1, "passage", 1, "08:20", "gps", "duk", False),
]


@pytest.fixture
def old_history(admin_url: str, tmp_path: Path) -> Iterator[psycopg.Connection]:
    """A database migrated up to 0019, with history in the old shape."""

    name = f"obehy_m_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    before = tmp_path / "migrations"
    before.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if int(path.name[:4]) < 20:
            shutil.copy(path, before / path.name)
    try:
        url = conninfo.make_conninfo(admin_url, dbname=name)
        with psycopg.connect(url, autocommit=True) as connection:
            migrate(connection, before)
            _record_old_history(connection)
            yield connection
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _record_old_history(connection: psycopg.Connection) -> None:
    for table in TABLES:
        connection.execute(
            sql.SQL(
                "CREATE TABLE history.{} PARTITION OF history.{}"
                " FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')"
            ).format(sql.Identifier(f"{table}_202610"), sql.Identifier(table))
        )
    connection.execute(
        "INSERT INTO history.derivation (core_version, policy_version, release_id)"
        " VALUES ('c', '12', 'run-a')"
    )
    for key, day in (("k1", "2026-10-08"), ("k2", "2026-10-09")):
        connection.execute(
            "INSERT INTO history.journey VALUES ('jdf', 'ns', %s, %s, %s, 1)",
            (key, day, f"{day} 07:00+02"),
        )
        connection.execute(
            "INSERT INTO history.journey_schedule (feed, key_namespace, key, service_date,"
            " revision, release_id, trip_id, route_name, headsign, derivation_id)"
            " VALUES ('jdf', 'ns', %s, %s, 1, 'run-a', %s, '1', 'C', 1)",
            (key, day, f"jdf:{key}"),
        )
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO history.journey_call VALUES"
            " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            CALLS,
        )
        cursor.executemany(
            "INSERT INTO history.actual_stop_event (feed, key_namespace, key, service_date,"
            " location_id, visit_n, event_type, revision, event_time, interval_lo, interval_hi,"
            " method, source, orphaned, derivation_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, (%s::date + %s::time) AT TIME ZONE 'UTC',"
            " (%s::date + %s::time) AT TIME ZONE 'UTC', (%s::date + %s::time) AT TIME ZONE 'UTC',"
            " %s, %s, %s, 1)",
            [(*e[:8], e[3], e[8], e[3], e[8], e[3], e[8], *e[9:]) for e in EVENTS],
        )
    connection.execute(
        "INSERT INTO history.vehicle_assignment VALUES"
        " ('jdf', 'duk', '1001', 'ns', 'k1', '2026-10-08', '2026-10-08 07:59+02',"
        " '2026-10-08 08:06+02', 'keyed', 1)"
    )
    connection.execute(
        "INSERT INTO history.journey_cancellation VALUES"
        " ('jdf', 'ns', 'k2', '2026-10-09', 'pid', '2026-10-09 07:00+02',"
        " '2026-10-09 07:00+02', NULL, 1)"
    )


def test_old_history_moves_to_slim_daily_tables(old_history: psycopg.Connection) -> None:
    connection = old_history
    migrate(connection)

    calls = connection.execute(
        "SELECT feed, key_namespace, key, service_date::text, revision, ordinal, location_id,"
        " visit_n, passenger_service, scheduled_arrival, scheduled_departure, name"
        " FROM history.named_call ORDER BY key, ordinal"
    ).fetchall()
    assert calls == CALLS
    events = connection.execute(
        "SELECT feed, key_namespace, key, service_date::text, location_id, visit_n, event_type,"
        " revision, to_char(event_time AT TIME ZONE 'UTC', 'HH24:MI'), method, source, orphaned"
        " FROM history.named_stop_event ORDER BY key, location_id"
    ).fetchall()
    assert events == [(*e[:8], e[8], *e[9:]) for e in EVENTS]

    days = connection.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = 'history.journey_call'::regclass ORDER BY 1"
    ).fetchall()
    assert days == [("journey_call_20261008",), ("journey_call_20261009",)]
    for table, rows in (
        ("journey", 2),
        ("journey_schedule", 2),
        ("vehicle_assignment", 1),
        ("journey_cancellation", 1),
        ("vehicle_day", 0),
    ):
        count = connection.execute(
            sql.SQL("SELECT count(*) FROM history.{}").format(sql.Identifier(table))
        ).fetchone()
        assert count == (rows,), table
    monthly = connection.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname LIKE 'history%' AND c.relname LIKE '%\\_202610'"
    ).fetchone()
    assert monthly == (0,)
    ids = connection.execute(
        "SELECT count(DISTINCT journey_id) FROM history.journey WHERE journey_id IS NOT NULL"
    ).fetchone()
    assert ids == (2,)
    # New journeys keep numbering after the moved ones.
    connection.execute(
        "INSERT INTO history.journey (feed, key_namespace, key, service_date, first_bound_at,"
        " latest_revision) VALUES ('jdf', 'ns', 'k3', '2026-10-09', now(), 0)"
    )
    assert connection.execute(
        "SELECT count(DISTINCT journey_id) FROM history.journey"
    ).fetchone() == (3,)
