"""Set-wise jobs over stored realtime data (`obehy jobs …`): vehicle days, retention and the
disk guard; SQL in `realtime/sql/`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql

from obehy.realtime.archive import days as archive_days
from obehy.realtime.archive import prune as prune_archive
from obehy.realtime.disk_guard import GB, next_step
from obehy.realtime.emit.storage import (
    HISTORY_TABLES,
    create_history_partitions,
    history_day,
    observation_end,
)
from obehy.realtime.policy import Policy

SQL = Path(__file__).resolve().parent / "sql"


def _query(name: str) -> LiteralString:
    # Files shipped with the package, never user input.
    return cast(LiteralString, (SQL / name).read_text(encoding="utf-8"))


def vehicle_day(connection: psycopg.Connection, day: date, policy: Policy) -> int:
    """Rebuild `history.vehicle_day` for one service date; returns the rows written."""

    create_history_partitions(connection, day)
    with connection.transaction():
        connection.execute("DELETE FROM history.vehicle_day WHERE service_date = %s", (day,))
        cursor = connection.execute(
            _query("vehicle_day.sql"), {"day": day, "gap_s": policy.time.vehicle_day_gap_s}
        )
        return cursor.rowcount


def _partitions(connection: psycopg.Connection, parent: str) -> list[str]:
    rows = connection.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = %s::regclass ORDER BY 1",
        (parent,),
    ).fetchall()
    return [cast(str, name) for (name,) in rows]


def drop_observations(connection: psycopg.Connection, before: datetime) -> list[str]:
    """Drop the `rt.observation` partitions (hourly, or daily from older servers) that end at
    or before `before`."""

    dropped: list[str] = []
    for name in _partitions(connection, "rt.observation"):
        end = observation_end(name)
        if end is not None and end <= before:
            connection.execute(sql.SQL("DROP TABLE rt.{}").format(sql.Identifier(name)))
            dropped.append(name)
    return dropped


def history_days(connection: psycopg.Connection) -> list[date]:
    """The service dates history has partitions of, oldest first."""

    found = {
        day
        for table in HISTORY_TABLES
        for name in _partitions(connection, f"history.{table}")
        if (day := history_day(name)) is not None
    }
    return sorted(found)


def drop_history(connection: psycopg.Connection, before: date) -> list[date]:
    """Drop every history table's partitions of service dates before `before`; returns the
    dates dropped."""

    dropped: set[date] = set()
    with connection.transaction():
        for table in HISTORY_TABLES:
            for name in _partitions(connection, f"history.{table}"):
                day = history_day(name)
                if day is not None and day < before:
                    connection.execute(
                        sql.SQL("DROP TABLE history.{}").format(sql.Identifier(name))
                    )
                    dropped.add(day)
    return sorted(dropped)


def hourly(
    connection: psycopg.Connection,
    archive: Path,
    policy: Policy,
    now: datetime,
    free: Callable[[], int],
) -> Iterator[str]:
    """Drop observation hours past the retention, then keep the data disk's free space above
    the guard; yields what it did. `free` measures the free bytes now."""

    cutoff = now - timedelta(hours=policy.retention.observation_hours)
    dropped = drop_observations(connection, cutoff)
    yield f"dropped {len(dropped)} observation partitions ending by {cutoff:%Y-%m-%d %H:%M} UTC"
    while True:
        available = free()
        step = next_step(available, policy.guard, archive_days(archive), history_days(connection))
        if step is None:
            if available < policy.guard.free_gb * GB:
                yield (
                    f"disk guard: {available / GB:.1f} GB free, below {policy.guard.free_gb} GB,"
                    " and nothing more may be deleted"
                )
            return
        if step.kind == "raw":
            prune_archive(archive, step.day + timedelta(days=1))
        else:
            drop_history(connection, step.day + timedelta(days=1))
        yield f"disk guard: {available / GB:.1f} GB free, deleted the {step.kind} day {step.day}"
