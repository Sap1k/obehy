"""Nightly set-wise jobs over history (`obehy jobs …`); SQL in `realtime/sql/`."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import LiteralString, cast

import psycopg

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
