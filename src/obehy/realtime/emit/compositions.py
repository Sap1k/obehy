"""Writing compositions (BASE_PLAN.md section 28): a source's answer about a journey's
formation, merged with the journey's earlier answers. Written by the lookup broker
(`runtime/lookups.py`); read by the API through `rt.composition_spans`."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from typing import Any, LiteralString, cast

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from obehy.realtime.compositions import JOURNEY_NAMESPACES, TripView, Unit, ViewCall
from obehy.realtime.times import Instant


def digest(units: Sequence[Unit]) -> str:
    """Tells identical answers apart from changed ones."""

    rows = [_unit_row(unit) for unit in units]
    text = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit_row(unit: Unit) -> dict[str, Any]:
    return {
        "sequence": unit.sequence,
        "kind": unit.kind,
        "role": unit.role,
        "vehicle_type": unit.vehicle_type,
        "coach_number": unit.coach_number,
        "classes": list(unit.classes),
        "features": list(unit.features),
        "texts": dict(unit.texts),
        "destination": unit.destination,
        "details": dict(unit.details),
    }


def journey_of(view: TripView) -> tuple[str, str, str, date]:
    for namespace in JOURNEY_NAMESPACES:
        key = view.keys.get(namespace)
        if key is not None:
            return view.feed, namespace, key, view.service_date
    return view.feed, "trip_id", view.trip_id, view.service_date


_INSERT_UNIT: LiteralString = (
    "INSERT INTO rt.composition_unit (composition_id, sequence, kind, role, vehicle_type,"
    " coach_number, classes, features, texts, destination, details)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def _write_units(
    connection: psycopg.Connection[DictRow], composition_id: int, units: Sequence[Unit]
) -> None:
    connection.execute(
        "DELETE FROM rt.composition_unit WHERE composition_id = %s", (composition_id,)
    )
    with connection.cursor() as cursor:
        cursor.executemany(
            _INSERT_UNIT,
            [
                (
                    composition_id,
                    u.sequence,
                    u.kind,
                    u.role,
                    u.vehicle_type,
                    u.coach_number,
                    list(u.classes),
                    list(u.features),
                    Jsonb(dict(u.texts)),
                    u.destination,
                    Jsonb(dict(u.details)),
                )
                for u in units
            ],
        )


def write_composition(
    connection: psycopg.Connection[DictRow],
    view: TripView,
    source: str,
    start: ViewCall,
    units: Sequence[Unit],
    at: Instant,
    fetch_id: int,
) -> int:
    """Store an answer holding from `start` onward, merged with the journey's earlier answers of
    the source (BASE_PLAN.md section 28): the same start replaces its units; an identical answer
    already holding there is extended; otherwise the answer holds until the next later start,
    and an earlier answer still holding at `start` ends there."""

    feed, namespace, key, service_date = journey_of(view)
    order = {(c.location_id, c.visit_n): c.ordinal for c in view.calls}
    new_digest = digest(units)
    with connection.transaction():
        rows = connection.execute(
            "SELECT composition_id, from_location, from_visit, to_location, to_visit, digest"
            " FROM rt.composition WHERE feed = %s AND key_namespace = %s AND key = %s"
            " AND service_date = %s AND source = %s FOR UPDATE",
            (feed, namespace, key, service_date, source),
        ).fetchall()

        def ordinal(location: str | None, visit: int | None) -> int | None:
            return None if location is None else order.get((location, cast(int, visit)))

        placed = sorted(
            (o, r) for r in rows if (o := ordinal(r["from_location"], r["from_visit"])) is not None
        )
        same = next((r for o, r in placed if o == start.ordinal), None)
        touch: LiteralString = (
            "UPDATE rt.composition SET observed_at = %s, fetch_id = %s, digest = %s"
            " WHERE composition_id = %s"
        )
        if same is not None:
            connection.execute(touch, (at, fetch_id, new_digest, same["composition_id"]))
            if same["digest"] != new_digest:
                _write_units(connection, same["composition_id"], units)
            return int(same["composition_id"])
        before = [r for o, r in placed if o < start.ordinal]
        after = [r for o, r in placed if o > start.ordinal]
        previous = before[-1] if before else None
        if previous is not None:
            until = ordinal(previous["to_location"], previous["to_visit"])
            holding = until is None or until > start.ordinal
            if previous["digest"] == new_digest and holding:
                connection.execute(touch, (at, fetch_id, new_digest, previous["composition_id"]))
                return int(previous["composition_id"])
            if holding:
                connection.execute(
                    "UPDATE rt.composition SET to_location = %s, to_visit = %s"
                    " WHERE composition_id = %s",
                    (start.location_id, start.visit_n, previous["composition_id"]),
                )
        following = after[0] if after else None
        created = connection.execute(
            "INSERT INTO rt.composition (feed, key_namespace, key, service_date, source,"
            " from_location, from_visit, to_location, to_visit, digest, first_seen, observed_at,"
            " fetch_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING composition_id",
            (
                feed,
                namespace,
                key,
                service_date,
                source,
                start.location_id,
                start.visit_n,
                None if following is None else following["from_location"],
                None if following is None else following["from_visit"],
                new_digest,
                at,
                at,
                fetch_id,
            ),
        ).fetchone()
        assert created is not None
        _write_units(connection, created["composition_id"], units)
        return int(created["composition_id"])
