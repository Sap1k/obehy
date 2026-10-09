"""`control.obehy_instant` agrees with `ServiceTime.instant` (docs/R1_SLICE.md section 2)."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from obehy.realtime.times import ServiceTime

pytestmark = pytest.mark.postgres

CASES = [
    ServiceTime(date(2026, 10, 8), 24 * 3600 + 1800),  # T1
    ServiceTime(date(2026, 3, 29), 5400),  # T6
    ServiceTime(date(2026, 3, 29), 9000),  # T6, skipped hour
    ServiceTime(date(2026, 3, 29), 18000),  # T6
    ServiceTime(date(2026, 10, 25), 9000),  # T7, repeated hour
    ServiceTime(date(2026, 10, 25), 18000),  # T7
    ServiceTime(date(2026, 10, 25), 3600),  # T14
    ServiceTime(date(2026, 10, 25), 14400),  # T14
    ServiceTime(date(2026, 10, 8), -1800),
]


@pytest.mark.parametrize("value", CASES, ids=lambda v: f"{v.service_date}+{v.seconds}")
def test_sql_instant_matches_python(connection: psycopg.Connection, value: ServiceTime) -> None:
    row = connection.execute(
        "SELECT control.obehy_instant(%s, %s)", (value.service_date, value.seconds)
    ).fetchone()
    assert row is not None
    assert row[0] == value.instant()
