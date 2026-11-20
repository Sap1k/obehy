"""The index rows read from a release's serving files (`index_files.FileRows`, replay without a
database) equal those read from PostgreSQL after `obehy release load` (`index_sql.SqlRows`).

Every `Rows` method is asked the same question both ways, over every identifier the fixture
release holds, so a rule `FileRows` re-implements in Python (visit numbers, the route name
fallback, on-request calls, the calendar's expansion into service dates, the train number)
cannot drift from its SQL. The order is compared where the `Rows` protocol promises one.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pyarrow.parquet as pq
import pytest

from obehy.realtime.index_files import FileRows
from obehy.realtime.index_sql import Row, SqlRows, run_loads
from obehy.release.contract import load_contract
from obehy.release.load import load_release
from tests.db.serving_fixture import czptt_rows, jdf_rows, write_release
from tests.realtime.builder import serving_rows, timetable

pytestmark = pytest.mark.postgres

DAY = date(2026, 10, 8)
NEXT = DAY + timedelta(days=1)


def _quiet(_: str) -> None:
    pass


def _column(serving: Path, table: str, column: str) -> list[Any]:
    path = serving / f"{table}.parquet"
    if not path.is_file():
        return []
    values: list[Any] = pq.read_table(path, columns=[column]).column(column).to_pylist()  # pyright: ignore[reportUnknownMemberType]
    return sorted({v for v in values if v is not None})


def _pairs(serving: Path) -> tuple[list[str], list[str]]:
    table = pq.read_table(serving / "source_key.parquet", columns=["namespace", "identifier"])  # pyright: ignore[reportUnknownMemberType]
    namespaces: list[str] = table.column("namespace").to_pylist()  # pyright: ignore[reportAssignmentType]
    identifiers: list[str] = table.column("identifier").to_pylist()  # pyright: ignore[reportAssignmentType]
    pairs = sorted(set(zip(namespaces, identifiers, strict=True)))
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _same(ordered: bool, sql: list[Row], files: list[Row]) -> None:
    left, right = [tuple(r) for r in sql], [tuple(r) for r in files]
    if not ordered:
        left, right = sorted(left, key=repr), sorted(right, key=repr)
    assert left == right


def _assert_parity(sql: SqlRows, files: FileRows, serving: Path) -> None:
    namespaces, identifiers = _pairs(serving)
    trip_ids = _column(serving, "trip", "trip_id")
    location_ids = _column(serving, "location", "location_id")
    shape_ids = _column(serving, "shape_point", "shape_id")
    service_ids = _column(serving, "trip", "service_id")
    run_keys = _column(serving, "trip", "run_key")
    lines = sorted({i.split(":", 1)[0] for i in identifiers})
    days = [date(2026, 10, 1) + timedelta(days=n) for n in range(16)]

    assert trip_ids, "the fixture feed has no trips"
    _same(True, sql.keys(namespaces, identifiers), files.keys(namespaces, identifiers))
    for namespace in sorted(set(namespaces)):
        _same(False, sql.line_keys(namespace, lines), files.line_keys(namespace, lines))
    _same(False, sql.trips(trip_ids), files.trips(trip_ids))
    _same(True, sql.calls(trip_ids), files.calls(trip_ids))
    _same(True, sql.run_parts(run_keys), files.run_parts(run_keys))
    _same(True, sql.station_keys(location_ids), files.station_keys(location_ids))
    codes = [i for n, i in zip(namespaces, identifiers, strict=True) if n == "sr70"]
    _same(True, sql.track_keys(codes), files.track_keys(codes))
    _same(False, sql.locations(location_ids), files.locations(location_ids))
    _same(True, sql.shape_points(shape_ids), files.shape_points(shape_ids))
    _same(False, sql.service_dates(service_ids, days), files.service_dates(service_ids, days))


def _check(connection: psycopg.Connection, release: Path) -> None:
    load_release(connection, release, load_contract(), report=_quiet)
    loads = run_loads(connection, release.name)
    assert loads is not None
    for feed in ("jdf", "czptt"):
        serving = release / feed / "serving"
        _assert_parity(SqlRows(connection, loads.load_ids[feed]), FileRows(serving), serving)


def test_fixture_release_reads_the_same_from_files_and_sql(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    """Weekday calendars with removed and added dates, posts placed exactly or not, on-request
    calls, a CZPTT run with its train number and SR70 tracks."""

    _check(connection, write_release(tmp_path, "run-parity", jdf=jdf_rows(), czptt=czptt_rows()))


def test_builder_release_reads_the_same_from_files_and_sql(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    """A trip visiting a stop twice across midnight (visit numbers), trips sharing a line."""

    tt = (
        timetable()
        .trip("582492:143", days=[DAY], calls=[("A", "23:50"), ("B", "24:05"), ("A", "24:20")])
        .trip("582492:144", days=[DAY, NEXT], calls=[("B", "06:00"), ("C", "06:20")])
        .trip("582493:1", days=[NEXT], calls=[("C", "07:00"), ("D", "07:10")])
    )
    _check(connection, write_release(tmp_path, "run-parity-tt", jdf=serving_rows(tt)))
