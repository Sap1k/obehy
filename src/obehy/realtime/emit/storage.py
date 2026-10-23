"""Where realtime rows are stored: the partitions of `rt.observation` and history, and the
history term dictionary (`history.term`).

`rt.observation` is partitioned per UTC hour (`observation_YYYYMMDDHH`) and every history table
per service date (`<table>_YYYYMMDD`), so retention drops whole partitions (`realtime/jobs.py`).
Older servers kept daily observation partitions (`observation_YYYYMMDD`); they are recognized
until dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import cast

import psycopg
from psycopg import errors, sql

HISTORY_TABLES = (
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

_OBSERVATION = re.compile(r"^observation_(\d{8})(\d{2})?$")
_HISTORY_DAY = re.compile(r"^(?P<table>[a-z_]+)_(?P<day>\d{8})$")


def hour_of(at: datetime) -> datetime:
    """The UTC hour an instant falls in, which names its observation partition."""

    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def observation_name(hour: datetime) -> str:
    return f"observation_{hour:%Y%m%d%H}"


def observation_end(name: str) -> datetime | None:
    """When an observation partition's range ends (hourly or the older daily), if it is one."""

    match = _OBSERVATION.match(name)
    if match is None:
        return None
    day = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=UTC)
    if match.group(2) is None:
        return day + timedelta(days=1)
    return day + timedelta(hours=int(match.group(2)) + 1)


def history_day(name: str) -> date | None:
    """The service date of a daily history partition, if `name` is one."""

    match = _HISTORY_DAY.match(name)
    if match is None or match.group("table") not in HISTORY_TABLES:
        return None
    day = match.group("day")
    return date(int(day[:4]), int(day[4:6]), int(day[6:]))


def create_observation_partition(connection: psycopg.Connection, hour: datetime) -> None:
    """The partition of one UTC hour. A daily partition of an older server already covers the
    hours of its day; the overlap is the sign."""

    try:
        with connection.transaction():
            connection.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS rt.{} PARTITION OF rt.observation"
                    " FOR VALUES FROM ({}) TO ({})"
                ).format(
                    sql.Identifier(observation_name(hour)),
                    sql.Literal(hour),
                    sql.Literal(hour + timedelta(hours=1)),
                )
            )
    except errors.InvalidObjectDefinition:
        pass


def create_history_partitions(connection: psycopg.Connection, day: date) -> None:
    """Every history table's partition of one service date."""

    for table in HISTORY_TABLES:
        connection.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS history.{} PARTITION OF history.{}"
                " FOR VALUES FROM ({}) TO ({})"
            ).format(
                sql.Identifier(f"{table}_{day:%Y%m%d}"),
                sql.Identifier(table),
                sql.Literal(day),
                sql.Literal(day + timedelta(days=1)),
            )
        )


@dataclass(slots=True)
class Terms:
    """`history.term`: the strings history keeps (stop IDs and names, methods, sources), each
    stored once and referred to by number. Missing ones are added outside the writer's
    transaction, so a rolled-back write never leaves a number known here but not stored."""

    ids: dict[str, int] = field(default_factory=dict[str, int])

    @classmethod
    def load(cls, connection: psycopg.Connection) -> Terms:
        rows = connection.execute("SELECT value, term_id FROM history.term").fetchall()
        return cls(dict(cast(list[tuple[str, int]], rows)))

    def add(self, connection: psycopg.Connection, values: Iterable[str]) -> None:
        missing = sorted({v for v in values if v not in self.ids})
        if not missing:
            return
        with connection.transaction():
            connection.execute(
                "INSERT INTO history.term (value) SELECT unnest(%s::text[])"
                " ON CONFLICT (value) DO NOTHING",
                (missing,),
            )
            rows = connection.execute(
                "SELECT value, term_id FROM history.term WHERE value = ANY(%s)", (missing,)
            ).fetchall()
        self.ids.update(cast(list[tuple[str, int]], rows))

    def __getitem__(self, value: str) -> int:
        return self.ids[value]
