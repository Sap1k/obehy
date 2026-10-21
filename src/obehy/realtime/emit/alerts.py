"""Alerts to PostgreSQL and to each feed's GTFS-RT (BASE_PLAN.md section 25).

`write_alerts` keeps `rt.alert` for one channel's poll, which states its whole current set:
each alert is upserted (`changed_at` moves only when its content changes, or it returns), and
the channel's alerts missing from the poll are withdrawn. `AlertFeed` serializes the GTFS-RT
`Alert` entities of each feed: the alerts not withdrawn and not over with at least one entity
resolved in that feed (`rt.alert_target`), informing only those entities, under our IDs. It
reloads after an alert poll or a release switch (`dirty`); entities are cached per alert.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
from google.transit import gtfs_realtime_pb2 as rt

from obehy.realtime.alerts import Alert, Texts
from obehy.realtime.model import Feed
from obehy.realtime.times import Instant, instant

SQL = Path(__file__).resolve().parent.parent / "sql"

# What a source says about an alert: a change of any of these moves `changed_at`.
_CONTENT: tuple[LiteralString, ...] = (
    "cause",
    "effect",
    "severity",
    "periods",
    "header",
    "description",
    "url",
    "cause_detail",
    "effect_detail",
    "entities",
    "details",
)


def _columns(prefix: LiteralString) -> LiteralString:
    return ", ".join(prefix + column for column in _CONTENT)


_MARKS: list[LiteralString] = ["%s"] * (len(_CONTENT) + 6)
_UPSERT: LiteralString = (
    "INSERT INTO rt.alert (source, alert_id, channel, " + _columns("") + ", first_seen,"
    " last_seen, changed_at) VALUES (" + ", ".join(_MARKS) + ")"
    " ON CONFLICT (source, alert_id) DO UPDATE SET"
    " changed_at = CASE WHEN rt.alert.withdrawn_at IS NOT NULL"
    " OR (" + _columns("rt.alert.") + ") IS DISTINCT FROM (" + _columns("excluded.") + ")"
    " THEN excluded.changed_at ELSE rt.alert.changed_at END,"
    " ("
    + _columns("")
    + ", channel, last_seen, withdrawn_at) = ("
    + _columns("excluded.")
    + ", excluded.channel, excluded.last_seen, NULL)"
)


def _texts(value: Texts) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)


def _row(alert: Alert, at: Instant) -> tuple[Any, ...]:
    periods = [
        {
            "start": None if p.start is None else p.start.isoformat(),
            "end": None if p.end is None else p.end.isoformat(),
        }
        for p in alert.periods
    ]
    entities = [
        {
            "kind": e.kind,
            "namespace": e.namespace,
            "identifier": e.identifier,
            "service_date": None if e.service_date is None else e.service_date.isoformat(),
        }
        for e in alert.entities
    ]
    return (
        alert.source,
        alert.alert_id,
        alert.channel,
        alert.cause,
        alert.effect,
        alert.severity,
        json.dumps(periods),
        _texts(alert.header),
        _texts(alert.description),
        _texts(alert.url),
        _texts(alert.cause_detail),
        _texts(alert.effect_detail),
        json.dumps(entities, ensure_ascii=False),
        json.dumps(dict(alert.details), ensure_ascii=False, sort_keys=True),
        at,
        at,
        at,
    )


def write_alerts(
    connection: psycopg.Connection,
    source: str,
    channel: str,
    alerts: Sequence[Alert],
    at: Instant,
) -> None:
    """One poll of an alert channel: its whole current set at `at`."""

    with connection.transaction(), connection.cursor() as cursor:
        if alerts:
            cursor.executemany(_UPSERT, [_row(alert, at) for alert in alerts])
        cursor.execute(
            "UPDATE rt.alert SET withdrawn_at = %s WHERE source = %s AND channel = %s"
            " AND withdrawn_at IS NULL AND NOT alert_id = ANY(%s)",
            (at, source, channel, [alert.alert_id for alert in alerts]),
        )


def prune_alerts(connection: psycopg.Connection, before: datetime) -> int:
    """Delete alerts withdrawn before `before`; how many."""

    deleted = connection.execute("DELETE FROM rt.alert WHERE withdrawn_at < %s", (before,))
    return deleted.rowcount


@cache
def _feed_query() -> LiteralString:
    return cast(LiteralString, (SQL / "alert_feed.sql").read_text(encoding="utf-8"))


def _translated(target: rt.TranslatedString, value: dict[str, str]) -> None:
    for language, text in sorted(value.items()):
        translation = target.translation.add()
        translation.text = text
        translation.language = language


def _epoch(value: str) -> int:
    return int(instant(datetime.fromisoformat(value)).timestamp())


def alert_entity(row: dict[str, Any]) -> bytes:
    """A GTFS-RT `Alert` entity of one alert row of `alert_feed.sql`."""

    entity = rt.FeedEntity()
    entity.id = f"alert:{row['source']}:{row['alert_id']}"
    alert = entity.alert
    for period in row["periods"]:
        active = alert.active_period.add()
        if period["start"] is not None:
            active.start = _epoch(period["start"])
        if period["end"] is not None:
            active.end = _epoch(period["end"])
    for target in row["targets"]:
        informed = alert.informed_entity.add()
        if target["kind"] == "route":
            informed.route_id = target["id"]
        elif target["kind"] == "stop":
            informed.stop_id = target["id"]
        else:
            informed.trip.trip_id = target["id"]
            if target["service_date"] is not None:
                informed.trip.start_date = target["service_date"].replace("-", "")
    # By name: the stubs type the enum fields as their enum classes, `Value` returns an int.
    alert.MergeFrom(
        rt.Alert(cause=row["cause"], effect=row["effect"], severity_level=row["severity"])
    )
    _translated(alert.header_text, row["header"])
    _translated(alert.description_text, row["description"])
    _translated(alert.url, row["url"])
    _translated(alert.cause_detail, row["cause_detail"])
    _translated(alert.effect_detail, row["effect_detail"])
    return entity.SerializeToString(deterministic=True)


def _over(periods: list[dict[str, str | None]], now: Instant) -> bool:
    ends = [p["end"] for p in periods]
    return bool(ends) and all(end is not None and _epoch(end) <= now.timestamp() for end in ends)


@dataclass(slots=True)
class AlertFeed:
    """Each feed's GTFS-RT alert entities, reloaded when `dirty`."""

    dirty: bool = True
    rows: dict[Feed, list[dict[str, Any]]] = field(default_factory=dict[Feed, list[dict[str, Any]]])
    cache: dict[tuple[Feed, str, str], tuple[str, bytes]] = field(
        default_factory=dict[tuple[Feed, str, str], tuple[str, bytes]]
    )

    def entities(self, connection: psycopg.Connection, feed: Feed, now: Instant) -> list[bytes]:
        if self.dirty:
            self._load(connection)
        out: list[bytes] = []
        for row in self.rows.get(feed, []):
            if _over(row["periods"], now):
                continue
            key = (feed, cast(str, row["source"]), cast(str, row["alert_id"]))
            version = json.dumps(row, sort_keys=True, default=str)
            cached = self.cache.get(key)
            if cached is None or cached[0] != version:
                cached = (version, alert_entity(row))
                self.cache[key] = cached
            out.append(cached[1])
        return out

    def _load(self, connection: psycopg.Connection) -> None:
        names = ("feed", "source", "alert_id", *_CONTENT, "targets")
        rows: dict[Feed, list[dict[str, Any]]] = {}
        for values in connection.execute(_feed_query()).fetchall():
            row: dict[str, Any] = dict(zip(names, values, strict=True))
            rows.setdefault(cast(Feed, row["feed"]), []).append(row)
        self.rows = rows
        live = {(f, r["source"], r["alert_id"]) for f, feed_rows in rows.items() for r in feed_rows}
        self.cache = {k: v for k, v in self.cache.items() if k in live}
        self.dirty = False
