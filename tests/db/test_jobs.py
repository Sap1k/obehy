"""`obehy jobs` and the history the API reads, against PostgreSQL: retention by the policy values
of `realtime_config`, over partitions and rows laid out as the Rust worker writes them
(`tests/db/worker_rows.py`)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest

from obehy.realtime.disk_guard import GB
from obehy.realtime.jobs import (
    archive_days,
    drop_history,
    drop_observations,
    history_days,
    hourly,
    nightly,
)
from obehy.realtime_config import Guard, policy_values
from tests.db.worker_rows import history_partitions, observation_partition

pytestmark = pytest.mark.postgres

D8, D9 = date(2026, 10, 8), date(2026, 10, 9)


def _scalar(connection: psycopg.Connection, query: LiteralString, *params: object) -> Any:
    row = connection.execute(query, params or None).fetchone()
    assert row is not None
    return row[0]


def _partitions(connection: psycopg.Connection, parent: str) -> list[str]:
    rows = connection.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = %s::regclass ORDER BY 1",
        (parent,),
    ).fetchall()
    return [r[0] for r in rows]


def _derivation(connection: psycopg.Connection) -> int:
    return _scalar(
        connection,
        "INSERT INTO history.derivation (core_version, policy_version, release_id)"
        " VALUES ('c', 'p', 'run-a') RETURNING derivation_id",
    )


def _assign(
    connection: psycopg.Connection, key: str, day: date, start: str, end: str, derivation: int
) -> None:
    """Bus duk:1001 on journey `key` of `day` from `start` to `end` (Prague wall time)."""

    history_partitions(connection, day)
    connection.execute(
        "INSERT INTO history.vehicle_assignment VALUES ('jdf', 'duk', '1001', 'cis:line_trip',"
        " %s, %s, (%s::timestamp AT TIME ZONE 'Europe/Prague'),"
        " (%s::timestamp AT TIME ZONE 'Europe/Prague'), 'keyed', %s, 'duk:1001')",
        (key, day, start, end, derivation),
    )


def _history(connection: psycopg.Connection) -> None:
    """A journey on each of D8 and D9, an event on D8's."""

    derivation = _derivation(connection)
    for day in (D8, D9):
        history_partitions(connection, day)
        connection.execute(
            "INSERT INTO history.journey (feed, key_namespace, key, service_date, first_bound_at,"
            " latest_revision) VALUES ('jdf', 'cis:line_trip', %s, %s, now(), 1)",
            (f"582492:{day.day}", day),
        )
    connection.execute("INSERT INTO history.term (value) VALUES ('A'), ('gps'), ('duk')")
    connection.execute(
        "INSERT INTO history.actual_stop_event (journey_id, service_date, location_term,"
        " visit_n, event_type, revision, event_time, interval_lo, interval_hi, method_term,"
        " source_term, derivation_id)"
        " SELECT j.journey_id, j.service_date,"
        " (SELECT term_id FROM history.term WHERE value = 'A'), 0, 'departure', 1, now(), now(),"
        " now(),"
        " (SELECT term_id FROM history.term WHERE value = 'gps'),"
        " (SELECT term_id FROM history.term WHERE value = 'duk'), %s"
        " FROM history.journey j WHERE j.service_date = %s",
        (derivation, D8),
    )


def test_nightly_prunes_each_sources_raw_days_by_its_window(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    archive = tmp_path / "rt-raw"
    for source, channel in (("pid", "vehicles"), ("sz-tabule", "boards")):
        for day in ("2026-11-01", "2026-11-19", "2026-11-20", "2026-11-21"):
            (archive / source / channel / day).mkdir(parents=True)

    lines = list(
        nightly(connection, archive, policy_values(), datetime(2026, 11, 21, 3, tzinfo=UTC))
    )

    assert lines[0] == "dropped 0 history days before 2026-10-31"
    assert lines[1].startswith("deleted 0 alerts withdrawn before 2026-11-07")
    assert lines[2].startswith("deleted 0 lookup requests before 2026-11-07")
    assert lines[3:] == [
        "deleted 2 raw archive days of pid before 2026-11-20",
        "deleted 1 raw archive days of sz-tabule before 2026-11-07",
    ]
    assert sorted(p.name for p in (archive / "pid" / "vehicles").iterdir()) == [
        "2026-11-20",
        "2026-11-21",
    ]


def test_retention_drops_observation_hours_and_older_daily_partitions(
    connection: psycopg.Connection,
) -> None:
    # A daily partition of an older server covers its day's hours.
    connection.execute(
        "CREATE TABLE rt.observation_20260908 PARTITION OF rt.observation"
        " FOR VALUES FROM ('2026-09-08 00:00Z') TO ('2026-09-09 00:00Z')"
    )
    for hour in (datetime(2026, 9, 9, 10, tzinfo=UTC), datetime(2026, 9, 9, 11, tzinfo=UTC)):
        observation_partition(connection, hour)
    assert _partitions(connection, "rt.observation") == [
        "observation_20260908",
        "observation_2026090910",
        "observation_2026090911",
    ]

    dropped = drop_observations(connection, datetime(2026, 9, 9, 11, tzinfo=UTC))
    assert dropped == ["observation_20260908", "observation_2026090910"]
    assert _partitions(connection, "rt.observation") == ["observation_2026090911"]


def test_the_history_window_drops_whole_days(connection: psycopg.Connection) -> None:
    _history(connection)
    assert history_days(connection) == [D8, D9]
    for day in (D8, D9):
        connection.execute(
            "WITH m AS (INSERT INTO rt.composition (feed, key_namespace, key, service_date,"
            " source, from_location, from_visit, digest, first_seen, observed_at, fetch_id)"
            " VALUES ('czptt', 'czptt:train_number', '6402', %s, 'cd', 'L1', 1, 'd', now(),"
            " now(), 1) RETURNING composition_id)"
            " INSERT INTO rt.composition_unit (composition_id, sequence, kind, role, classes,"
            " features, texts, details) SELECT composition_id, 1, 'unit', 'train', '{}', '{}',"
            " '{}', '{}' FROM m",
            (day,),
        )

    assert drop_history(connection, D9) == [D8]
    # Compositions are kept as long as history.
    assert _scalar(connection, "SELECT count(*) FROM rt.composition") == 1
    assert _scalar(connection, "SELECT count(*) FROM rt.composition_unit") == 1
    assert history_days(connection) == [D9]
    assert _scalar(connection, "SELECT count(*) FROM history.actual_stop_event") == 0
    assert _scalar(connection, "SELECT count(*) FROM history.journey") == 1
    assert "journey_call_20261008" not in _partitions(connection, "history.journey_call")


def test_the_disk_guard_deletes_raw_days_then_history_days(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    _history(connection)
    archive = tmp_path / "rt-raw"
    for day in range(4, 9):
        (archive / "duk" / "vehicles" / f"2026-10-0{day}").mkdir(parents=True)
    policy = replace(policy_values(), guard=Guard(free_gb=6, min_raw_days=3, min_history_days=1))

    lines = list(
        hourly(connection, archive, policy, datetime(2026, 10, 9, 12, tzinfo=UTC), lambda: 0)
    )

    assert archive_days(archive) == [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    assert history_days(connection) == [D9]
    assert [line.split(", ", 1)[1] for line in lines[1:-1]] == [
        "deleted the raw day 2026-10-04",
        "deleted the raw day 2026-10-05",
        "deleted the history day 2026-10-08",
    ]
    assert "nothing more may be deleted" in lines[-1]
    # With room enough nothing goes.
    assert (
        list(
            hourly(
                connection, archive, policy, datetime(2026, 10, 9, 12, tzinfo=UTC), lambda: 10 * GB
            )
        )[1:]
        == []
    )


def test_t13_a_vehicle_day_crosses_midnight_and_splits_at_long_gaps(
    connection: psycopg.Connection,
) -> None:
    derivation = _derivation(connection)
    _assign(connection, "582492:1", D8, "2026-10-08 22:00", "2026-10-08 23:30", derivation)
    _assign(connection, "582492:2", D9, "2026-10-09 00:15", "2026-10-09 01:00", derivation)
    _assign(connection, "582492:3", D9, "2026-10-09 06:00", "2026-10-09 07:00", derivation)

    gap_s = policy_values().vehicle_day_gap_s
    rows = [
        row
        for day in (D8, D9)
        for row in connection.execute(
            "SELECT vehicle_id, service_date, seq, jsonb_array_length(journeys)"
            " FROM history.vehicle_chains(%s, %s) ORDER BY seq",
            (day, gap_s),
        ).fetchall()
    ]
    # Dated by its first journey; the history row names the vehicle the worker resolved.
    assert rows == [("duk:1001", D8, 1, 2), ("duk:1001", D9, 1, 1)]
    assert (
        connection.execute(
            "SELECT * FROM history.vehicle_chains(%s, %s, vehicle => 'duk:1002')", (D8, gap_s)
        ).fetchall()
        == []
    )
