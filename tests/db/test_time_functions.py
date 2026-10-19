"""`control.obehy_instant` and its fast form agree with `ServiceTime.instant` (docs/R1_SLICE.md
section 2)."""

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
    ServiceTime(date(2026, 10, 24), 26 * 3600 + 1800),  # the next night's repeated hour
    ServiceTime(date(2026, 3, 28), 26 * 3600 + 1800),  # the next night's skipped hour
]


@pytest.mark.parametrize("value", CASES, ids=lambda v: f"{v.service_date}+{v.seconds}")
def test_sql_instant_matches_python(connection: psycopg.Connection, value: ServiceTime) -> None:
    row = connection.execute(
        "SELECT control.obehy_instant(%(d)s, %(s)s), control.obehy_instant_fast(%(d)s, %(s)s)",
        {"d": value.service_date, "s": value.seconds},
    ).fetchone()
    assert row is not None
    assert row[0] == row[1] == value.instant()


def test_the_fast_instant_matches_every_minute_around_both_changes(
    connection: psycopg.Connection,
) -> None:
    for day in (date(2026, 3, 28), date(2026, 3, 29), date(2026, 10, 24), date(2026, 10, 25)):
        values = [ServiceTime(day, s) for s in range(-3600, 48 * 3600, 60)]
        rows = connection.execute(
            "SELECT control.obehy_instant_fast(%s, s) FROM unnest(%s::integer[]) AS s",
            (day, [v.seconds for v in values]),
        ).fetchall()
        assert [r[0] for r in rows] == [v.instant() for v in values], day
