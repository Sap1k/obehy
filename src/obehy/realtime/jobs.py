"""Nightly set-wise jobs over history (`obehy jobs …`); SQL in `realtime/sql/`."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql

from obehy.realtime.policy import Policy

SQL = Path(__file__).resolve().parent / "sql"


def _query(name: str) -> LiteralString:
    # Files shipped with the package, never user input.
    return cast(LiteralString, (SQL / name).read_text(encoding="utf-8"))


def vehicle_day(connection: psycopg.Connection, day: date, policy: Policy) -> int:
    """Rebuild `history.vehicle_day` for one service date; returns the rows written."""

    with connection.transaction():
        connection.execute("DELETE FROM history.vehicle_day WHERE service_date = %s", (day,))
        cursor = connection.execute(
            _query("vehicle_day.sql"), {"day": day, "gap_s": policy.time.vehicle_day_gap_s}
        )
        return cursor.rowcount


# Daily partitions are named by the writer (`emit.db.Writer`) after their UTC day.
_OBSERVATION_DAY = re.compile(r"^observation_(\d{8})$")


def drop_observations(connection: psycopg.Connection, before: date) -> list[str]:
    """Drop the daily `rt.observation` partitions of UTC days before `before`."""

    partitions = connection.execute(
        "SELECT c.relname FROM pg_inherits i"
        " JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = 'rt.observation'::regclass ORDER BY 1"
    ).fetchall()
    dropped: list[str] = []
    for (name,) in partitions:
        match = _OBSERVATION_DAY.match(cast(str, name))
        if match is None:
            continue
        if date.fromisoformat(match.group(1)) < before:
            connection.execute(sql.SQL("DROP TABLE rt.{}").format(sql.Identifier(name)))
            dropped.append(cast(str, name))
    return dropped
