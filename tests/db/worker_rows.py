"""Rows the realtime worker (`obehy-rt`, Rust) writes, written here with plain SQL for the tests of
what reads them: the API and the jobs. The shapes are the worker's (`rust/obehy-rt/src/store/`);
nothing here decides anything the worker decides."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast

import psycopg
from psycopg import sql

from obehy.realtime.jobs import HISTORY_TABLES
from obehy.realtime_config import SOURCES


def physical_rows() -> list[tuple[str, str, bool]]:
    """Each manifest's `[vehicles] physical` by mode (`default` true unless it says otherwise):
    `rt.physical_vehicles`, as the worker writes it at start."""

    rows: list[tuple[str, str, bool]] = []
    for path in sorted(SOURCES.glob("*.toml")):
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        vehicles = cast(dict[str, Any], document.get("vehicles", {}))
        physical = {"default": True, **cast(dict[str, bool], vehicles.get("physical", {}))}
        rows += [(cast(str, document["source"]), mode, on) for mode, on in sorted(physical.items())]
    return sorted(rows)


def write_physical_policy(connection: psycopg.Connection) -> None:
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM rt.physical_vehicles")
        cursor.executemany(
            "INSERT INTO rt.physical_vehicles (source, mode, physical) VALUES (%s, %s, %s)",
            physical_rows(),
        )


@dataclass(frozen=True, slots=True)
class Alert:
    """One `rt.alert` row as an alert channel's poll leaves it (texts by language)."""

    source: str
    channel: str
    alert_id: str
    cause: str
    effect: str
    severity: str
    header: dict[str, str]
    description: dict[str, str] = field(default_factory=dict[str, str])
    entities: tuple[tuple[str, str, str], ...] = ()  # (kind, namespace, identifier)
    details: dict[str, str] = field(default_factory=dict[str, str])


def write_alerts(
    connection: psycopg.Connection, alerts: Sequence[Alert], at: datetime | None = None
) -> None:
    """Alerts current at `at` (the database's now by default), each new or replaced."""

    for alert in alerts:
        entities = [
            {"kind": kind, "namespace": namespace, "identifier": identifier, "service_date": None}
            for kind, namespace, identifier in alert.entities
        ]
        connection.execute(
            "INSERT INTO rt.alert (source, alert_id, channel, cause, effect, severity, periods,"
            " header, description, url, cause_detail, effect_detail, entities, details,"
            " first_seen, last_seen, changed_at) VALUES (%(source)s, %(alert_id)s, %(channel)s,"
            ' %(cause)s, %(effect)s, %(severity)s, \'[{"start": null, "end": null}]\','
            " %(header)s, %(description)s, '{}', '{}', '{}', %(entities)s, %(details)s,"
            " coalesce(%(at)s, now()), coalesce(%(at)s, now()), coalesce(%(at)s, now()))"
            " ON CONFLICT (source, alert_id) DO UPDATE SET (cause, effect, severity, header,"
            " description, entities, details, last_seen, changed_at, withdrawn_at) ="
            " (excluded.cause, excluded.effect, excluded.severity, excluded.header,"
            " excluded.description, excluded.entities, excluded.details, excluded.last_seen,"
            " excluded.changed_at, NULL)",
            {
                "source": alert.source,
                "alert_id": alert.alert_id,
                "channel": alert.channel,
                "cause": alert.cause,
                "effect": alert.effect,
                "severity": alert.severity,
                "header": json.dumps(alert.header, ensure_ascii=False, sort_keys=True),
                "description": json.dumps(alert.description, ensure_ascii=False, sort_keys=True),
                "entities": json.dumps(entities, ensure_ascii=False),
                "details": json.dumps(alert.details, ensure_ascii=False, sort_keys=True),
                "at": at,
            },
        )


def history_partitions(connection: psycopg.Connection, day: date) -> None:
    """Every history table's partition of one service date, named as the worker names them."""

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


def observation_partition(connection: psycopg.Connection, hour: datetime) -> None:
    """The `rt.observation` partition of one UTC hour, named as the worker names it."""

    connection.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS rt.{} PARTITION OF rt.observation"
            " FOR VALUES FROM ({}) TO ({})"
        ).format(
            sql.Identifier(f"observation_{hour:%Y%m%d%H}"),
            sql.Literal(hour),
            sql.Literal(hour + timedelta(hours=1)),
        )
    )
