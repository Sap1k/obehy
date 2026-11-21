"""The writer's rows to PostgreSQL (`emit/rows.py`): `rt.observation`, history, current state.

Partitions are created on demand: `rt.observation` per UTC hour, history per service date
(`emit/storage.py`). History rows get the writer's `derivation_id`, so `clear_history` followed
by a replay rebuilds days after a fix (BASE_PLAN.md section 29).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import cast

import psycopg
from psycopg import sql

from obehy.realtime.emit.rows import Row, StateTable
from obehy.realtime.emit.storage import (
    HISTORY_TABLES,
    Terms,
    create_history_partitions,
    create_observation_partition,
)
from obehy.realtime.emit.vehicles import (
    VehicleBook,
    VehicleRows,
    write_physical_policy,
    write_vehicle_rows,
)
from obehy.realtime.model import Derivation, SnapshotJourney


@dataclass(slots=True)
class SqlStore:
    connection: psycopg.Connection
    derivation_id: int = 0
    terms: Terms = field(default_factory=Terms)
    _observation_hours: set[datetime] = field(default_factory=set[datetime])
    _history_days: set[date] = field(default_factory=set[date])

    def open(self, derivation: Derivation) -> VehicleBook:
        d = derivation
        self.connection.execute(
            "INSERT INTO history.derivation (core_version, policy_version, release_id)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (d.core_version, d.policy_version, d.release_id),
        )
        row = self.connection.execute(
            "SELECT derivation_id FROM history.derivation"
            " WHERE core_version = %s AND policy_version = %s AND release_id = %s",
            (d.core_version, d.policy_version, d.release_id),
        ).fetchone()
        assert row is not None
        self.derivation_id = cast(int, row[0])
        self.terms = Terms.load(self.connection)
        book = VehicleBook.load(self.connection)
        write_physical_policy(self.connection)
        return book

    def reload_registry(self, book: VehicleBook) -> None:
        book.reload(self.connection)

    # --- partitions and terms -------------------------------------------------------------------

    def prepare(self, hours: set[datetime], days: set[date], terms: Iterable[str]) -> None:
        for hour in hours:
            if hour not in self._observation_hours:
                create_observation_partition(self.connection, hour)
                self._observation_hours.add(hour)
        for day in days:
            if day not in self._history_days:
                create_history_partitions(self.connection, day)
                self._history_days.add(day)
        self.terms.add(self.connection, terms)

    def transaction(self) -> AbstractContextManager[object]:
        return self.connection.transaction()

    # --- effects -------------------------------------------------------------------------------

    def observations(self, rows: Sequence[Row]) -> None:
        if not rows:
            return
        with self.connection.cursor().copy(
            "COPY rt.observation (received_at, source, channel, feed, raw_sha256, raw_item,"
            " observed_at, decoder_version, fact_schema_version, facts, journey_namespace,"
            " journey_key, service_date, reason, release_id, vehicle_id, state_since) FROM STDIN"
        ) as copy:
            for row in rows:
                facts = json.dumps(row[9], separators=(",", ":"))
                copy.write_row((*row[:9], facts, *row[10:]))

    def snapshot(self, snapshot: SnapshotJourney) -> None:
        j = snapshot.journey
        key = (j.feed, j.namespace, j.key, j.service_date)
        self.connection.execute(
            "INSERT INTO history.journey (feed, key_namespace, key, service_date,"
            " first_bound_at, latest_revision) VALUES (%s, %s, %s, %s, %s, 0)"
            " ON CONFLICT DO NOTHING",
            (*key, snapshot.at),
        )
        row = self.connection.execute(
            "SELECT j.journey_id, j.latest_revision, s.release_id, s.trip_id"
            " FROM history.journey j"
            " LEFT JOIN history.journey_schedule s USING (feed, key_namespace, key, service_date)"
            " WHERE (j.feed, j.key_namespace, j.key, j.service_date) = (%s, %s, %s, %s)"
            " AND (s.revision IS NULL OR s.revision = j.latest_revision)",
            key,
        ).fetchone()
        assert row is not None
        journey_id, latest, release_id, trip_id = cast(tuple[int, int, str | None, str | None], row)
        day = snapshot.journey.service_date
        if (release_id, trip_id) == (snapshot.release_id, snapshot.trip_id):
            return
        revision = latest + 1
        self.connection.execute(
            "INSERT INTO history.journey_schedule (feed, key_namespace, key, service_date,"
            " revision, release_id, trip_id, route_name, headsign, derivation_id, run_key,"
            " route_id, mode, agency_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                *key,
                revision,
                snapshot.release_id,
                snapshot.trip_id,
                snapshot.route_name,
                snapshot.headsign,
                self.derivation_id,
                snapshot.run_key,
                snapshot.route_id,
                snapshot.mode,
                snapshot.agency_id,
            ),
        )
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.journey_call (journey_id, service_date, revision, ordinal,"
                " location_term, visit_n, passenger_service, scheduled_arrival,"
                " scheduled_departure, name_term)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        journey_id,
                        day,
                        revision,
                        c.ordinal,
                        self.terms[c.location_id],
                        c.visit_n,
                        c.passenger_service,
                        c.scheduled_arrival,
                        c.scheduled_departure,
                        self.terms[c.name],
                    )
                    for c in snapshot.calls
                ],
            )
        self.connection.execute(
            "UPDATE history.journey SET latest_revision = %s"
            " WHERE (feed, key_namespace, key, service_date) = (%s, %s, %s, %s)",
            (revision, *key),
        )
        if latest > 0:
            # Re-attach events by (location_id, visit_n); a vanished call orphans its events.
            self.connection.execute(
                "UPDATE history.actual_stop_event e SET"
                " orphaned = NOT EXISTS (SELECT 1 FROM history.journey_call c"
                "   WHERE (c.journey_id, c.service_date, c.revision, c.location_term, c.visit_n)"
                "       = (e.journey_id, e.service_date, %s, e.location_term, e.visit_n)),"
                " revision = CASE WHEN EXISTS (SELECT 1 FROM history.journey_call c"
                "   WHERE (c.journey_id, c.service_date, c.revision, c.location_term, c.visit_n)"
                "       = (e.journey_id, e.service_date, %s, e.location_term, e.visit_n))"
                "   THEN %s ELSE e.revision END"
                " WHERE (e.journey_id, e.service_date) = (%s, %s)",
                (revision, revision, revision, journey_id, day),
            )

    def events(self, rows: Sequence[Row]) -> None:
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.actual_stop_event (journey_id, service_date, location_term,"
                " visit_n, event_type, revision, event_time, interval_lo, interval_hi,"
                " method_term, source_term, derivation_id)"
                " SELECT j.journey_id, j.service_date, %s, %s, %s::history.event_kind,"
                " j.latest_revision, %s, %s, %s, %s, %s, %s"
                " FROM history.journey j"
                " WHERE (j.feed, j.key_namespace, j.key, j.service_date) = (%s, %s, %s, %s)"
                " ON CONFLICT (journey_id, service_date, location_term, visit_n, event_type)"
                " DO UPDATE SET revision = excluded.revision,"
                " event_time = excluded.event_time, interval_lo = excluded.interval_lo,"
                " interval_hi = excluded.interval_hi, method_term = excluded.method_term,"
                " source_term = excluded.source_term, orphaned = false,"
                " derivation_id = excluded.derivation_id",
                [
                    (
                        self.terms[location],
                        visit_n,
                        kind,
                        mid,
                        lo,
                        hi,
                        self.terms[method],
                        self.terms[source],
                        self.derivation_id,
                        feed,
                        namespace,
                        key,
                        day,
                    )
                    for (
                        feed,
                        namespace,
                        key,
                        day,
                        location,
                        visit_n,
                        kind,
                        mid,
                        lo,
                        hi,
                        method,
                        source,
                    ) in rows
                ],
            )

    def assignments(self, rows: Sequence[Row]) -> None:
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.vehicle_assignment (feed, source, source_vehicle_id,"
                " key_namespace, key, service_date, first_seen, last_seen, method,"
                " derivation_id, vehicle_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (feed, source, source_vehicle_id, key_namespace, key,"
                " service_date) DO UPDATE SET"
                " first_seen = least(history.vehicle_assignment.first_seen, excluded.first_seen),"
                " last_seen = greatest(history.vehicle_assignment.last_seen, excluded.last_seen),"
                " vehicle_id = excluded.vehicle_id",
                [(*row[:9], self.derivation_id, row[9]) for row in rows],
            )

    def links(self, rows: Sequence[Row]) -> None:
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.journey_link (feed, source_namespace, source_key,"
                " target_namespace, target_key, service_date, kind, derivation_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [(*row, self.derivation_id) for row in rows],
            )

    def cancellations(self, rows: Sequence[Row]) -> None:
        """A cancellation is kept per journey and source (first and last said); a vehicle seen
        running the journey after all restores it, whichever source cancelled it, until a
        later cancellation."""

        for feed, namespace, key, day, source, at, cancelled in rows:
            if cancelled:
                self.connection.execute(
                    "INSERT INTO history.journey_cancellation (feed, key_namespace, key,"
                    " service_date, source, first_seen, last_seen, derivation_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (feed, key_namespace, key, service_date, source) DO UPDATE SET"
                    " first_seen = least(history.journey_cancellation.first_seen,"
                    " excluded.first_seen),"
                    " last_seen = greatest(history.journey_cancellation.last_seen,"
                    " excluded.last_seen),"
                    " restored_at = CASE WHEN history.journey_cancellation.restored_at"
                    " < excluded.last_seen THEN NULL"
                    " ELSE history.journey_cancellation.restored_at END",
                    (feed, namespace, key, day, source, at, at, self.derivation_id),
                )
            else:
                self.connection.execute(
                    "UPDATE history.journey_cancellation SET restored_at = %s"
                    " WHERE (feed, key_namespace, key, service_date) = (%s, %s, %s, %s)"
                    " AND restored_at IS NULL AND last_seen <= %s",
                    (at, feed, namespace, key, day, at),
                )

    def platforms(self, rows: Sequence[Row]) -> None:
        if not rows:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.platform_evidence (feed, key_namespace, key, service_date,"
                " location_id, visit_n, event_type, value, label, boarding_point_id, source,"
                " first_seen, last_seen, derivation_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (feed, key_namespace, key, service_date, location_id, visit_n,"
                " event_type, value) DO UPDATE SET"
                " first_seen = least(history.platform_evidence.first_seen, excluded.first_seen),"
                " last_seen = greatest(history.platform_evidence.last_seen, excluded.last_seen),"
                " boarding_point_id = excluded.boarding_point_id, label = excluded.label",
                [(*row, self.derivation_id) for row in rows],
            )

    def vehicles(self, rows: VehicleRows) -> None:
        write_vehicle_rows(self.connection, rows)

    # --- current state and rebuilds -------------------------------------------------------------

    def replace_state(self, table: StateTable, feed: str, rows: Sequence[Row]) -> None:
        name = sql.Identifier(table.name_text)
        columns = sql.SQL(", ").join(sql.Identifier(c) for c in table.columns)
        self.connection.execute(sql.SQL("DELETE FROM rt.{} WHERE feed = %s").format(name), (feed,))
        with self.connection.cursor().copy(
            sql.SQL("COPY rt.{} ({}) FROM STDIN").format(name, columns)
        ) as copy:
            for row in rows:
                copy.write_row(row)

    def delete_state(self, table: StateTable, keys: Sequence[Row]) -> None:
        self.connection.execute(
            sql.SQL("DELETE FROM rt.{} WHERE ({}) IN (SELECT * FROM unnest({}))").format(
                sql.Identifier(table.name_text),
                sql.SQL(", ").join(sql.Identifier(c) for c in table.key),
                sql.SQL(", ").join(sql.SQL("%s::{}[]").format(sql.SQL(t)) for t in table.key_types),
            ),
            [list(column) for column in zip(*keys, strict=True)],
        )

    def upsert_state(self, table: StateTable, rows: Sequence[Row]) -> None:
        name = sql.Identifier(table.name_text)
        columns = sql.SQL(", ").join(sql.Identifier(c) for c in table.columns)
        stage = sql.Identifier(f"{table.name_text}_stage")
        self.connection.execute(
            sql.SQL("CREATE TEMP TABLE IF NOT EXISTS {} (LIKE rt.{}) ON COMMIT DELETE ROWS").format(
                stage, name
            )
        )
        with self.connection.cursor().copy(
            sql.SQL("COPY {} ({}) FROM STDIN").format(stage, columns)
        ) as copy:
            for row in rows:
                copy.write_row(row)
        updates = [c for c in table.columns if c not in table.key]
        self.connection.execute(
            sql.SQL(
                "INSERT INTO rt.{} ({}) SELECT {} FROM {}"
                " ON CONFLICT ({}) DO UPDATE SET ({}) = ({})"
            ).format(
                name,
                columns,
                columns,
                stage,
                sql.SQL(", ").join(sql.Identifier(c) for c in table.key),
                sql.SQL(", ").join(sql.Identifier(c) for c in updates),
                sql.SQL(", ").join(
                    sql.SQL("EXCLUDED.{}").format(sql.Identifier(c)) for c in updates
                ),
            )
        )

    def clear_history(self, days: Sequence[date]) -> None:
        """Delete the history of service dates before a rebuild by replay."""

        if not days:
            return
        with self.connection.transaction():
            for table in HISTORY_TABLES:
                self.connection.execute(
                    sql.SQL("DELETE FROM history.{} WHERE service_date = ANY(%s)").format(
                        sql.Identifier(table)
                    ),
                    (list(days),),
                )


def clear_observations(
    connection: psycopg.Connection, start: datetime, end: datetime, sources: Sequence[str]
) -> None:
    """Delete stored observations of `sources` received in [start, end) before a replay."""

    connection.execute(
        "DELETE FROM rt.observation WHERE received_at >= %s AND received_at < %s"
        " AND source = ANY(%s)",
        (start, end, list(sources)),
    )
