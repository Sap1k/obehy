"""Execute core effects against PostgreSQL: `rt.observation`, history, current state.

Partitions are created on demand: `rt.observation` per UTC day, history per service-date
month. Each `write` is one transaction. History rows carry the writer's derivation, so
`clear_history` followed by a replay rebuilds days after a fix (BASE_PLAN.md section 29).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import psycopg
from psycopg import sql

from obehy.realtime.model import (
    AssignVehicle,
    CallState,
    Derivation,
    Effect,
    Feed,
    FeedState,
    JourneyKey,
    Observation,
    ObservationResult,
    RawRef,
    SnapshotJourney,
    VehicleKey,
    WriteEvent,
)
from obehy.realtime.model_json import facts_from_json, facts_to_json
from obehy.realtime.times import instant

HISTORY_TABLES = (
    "journey",
    "journey_schedule",
    "journey_call",
    "actual_stop_event",
    "vehicle_assignment",
    "vehicle_day",
)


def _key(journey: JourneyKey) -> tuple[str, str, str, date]:
    return journey.feed, journey.namespace, journey.key, journey.service_date


def _month(day: date) -> date:
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


@dataclass(slots=True)
class Writer:
    connection: psycopg.Connection
    derivation: Derivation
    derivation_id: int = 0
    _observation_days: set[date] = field(default_factory=set[date])
    _history_months: set[date] = field(default_factory=set[date])

    def __post_init__(self) -> None:
        d = self.derivation
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

    # --- partitions ---------------------------------------------------------------------------

    def _observation_partition(self, day: date) -> None:
        if day in self._observation_days:
            return
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        self.connection.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS rt.{} PARTITION OF rt.observation"
                " FOR VALUES FROM ({}) TO ({})"
            ).format(
                sql.Identifier(f"observation_{day:%Y%m%d}"),
                sql.Literal(start),
                sql.Literal(start + timedelta(days=1)),
            )
        )
        self._observation_days.add(day)

    def _history_partition(self, day: date) -> None:
        month = _month(day)
        if month in self._history_months:
            return
        for table in HISTORY_TABLES:
            self.connection.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS history.{} PARTITION OF history.{}"
                    " FOR VALUES FROM ({}) TO ({})"
                ).format(
                    sql.Identifier(f"{table}_{month:%Y%m}"),
                    sql.Identifier(table),
                    sql.Literal(month),
                    sql.Literal(_next_month(month)),
                )
            )
        self._history_months.add(month)

    # --- effects -------------------------------------------------------------------------------

    def write(self, effects: Sequence[Effect]) -> None:
        results = [e for e in effects if isinstance(e, ObservationResult)]
        snapshots = [e for e in effects if isinstance(e, SnapshotJourney)]
        events = [e for e in effects if isinstance(e, WriteEvent)]
        assigned = [e for e in effects if isinstance(e, AssignVehicle)]
        for result in results:
            self._observation_partition(result.observation.received_at.astimezone(UTC).date())
        for day in {e.journey.service_date for e in snapshots}:
            self._history_partition(day)
        with self.connection.transaction():
            self._observations(results)
            for snapshot in snapshots:
                self._snapshot(snapshot)
            self._events(events)
            self._assignments(results, assigned)

    def _observations(self, results: Sequence[ObservationResult]) -> None:
        if not results:
            return
        with self.connection.cursor().copy(
            "COPY rt.observation (received_at, source, channel, feed, raw_sha256, raw_item,"
            " observed_at, decoder_version, fact_schema_version, facts, journey_namespace,"
            " journey_key, service_date, reason, release_id) FROM STDIN"
        ) as copy:
            for result in results:
                obs = result.observation
                facts = facts_to_json(obs.facts)
                journey = result.journey
                copy.write_row(
                    (
                        obs.received_at,
                        obs.source,
                        obs.channel,
                        obs.feed,
                        obs.raw.sha256,
                        obs.raw.item,
                        obs.observed_at,
                        obs.decoder_version,
                        facts["v"],
                        json.dumps(facts, separators=(",", ":")),
                        None if journey is None else journey.namespace,
                        None if journey is None else journey.key,
                        None if journey is None else journey.service_date,
                        None if result.reason is None else result.reason.value,
                        self.derivation.release_id,
                    )
                )

    def _snapshot(self, snapshot: SnapshotJourney) -> None:
        key = _key(snapshot.journey)
        self.connection.execute(
            "INSERT INTO history.journey (feed, key_namespace, key, service_date,"
            " first_bound_at, latest_revision) VALUES (%s, %s, %s, %s, %s, 0)"
            " ON CONFLICT DO NOTHING",
            (*key, snapshot.at),
        )
        row = self.connection.execute(
            "SELECT j.latest_revision, s.release_id, s.trip_id FROM history.journey j"
            " LEFT JOIN history.journey_schedule s USING (feed, key_namespace, key, service_date)"
            " WHERE (j.feed, j.key_namespace, j.key, j.service_date) = (%s, %s, %s, %s)"
            " AND (s.revision IS NULL OR s.revision = j.latest_revision)",
            key,
        ).fetchone()
        assert row is not None
        latest, release_id, trip_id = cast(tuple[int, str | None, str | None], row)
        if (release_id, trip_id) == (snapshot.release_id, snapshot.trip_id):
            return
        revision = latest + 1
        self.connection.execute(
            "INSERT INTO history.journey_schedule (feed, key_namespace, key, service_date,"
            " revision, release_id, trip_id, route_name, headsign, derivation_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                *key,
                revision,
                snapshot.release_id,
                snapshot.trip_id,
                snapshot.route_name,
                snapshot.headsign,
                self.derivation_id,
            ),
        )
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.journey_call (feed, key_namespace, key, service_date,"
                " revision, ordinal, location_id, visit_n, passenger_service,"
                " scheduled_arrival, scheduled_departure, name)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        *key,
                        revision,
                        c.ordinal,
                        c.location_id,
                        c.visit_n,
                        c.passenger_service,
                        c.scheduled_arrival,
                        c.scheduled_departure,
                        c.name,
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
                "   WHERE (c.feed, c.key_namespace, c.key, c.service_date, c.revision,"
                "          c.location_id, c.visit_n)"
                "       = (e.feed, e.key_namespace, e.key, e.service_date, %s,"
                "          e.location_id, e.visit_n)),"
                " revision = CASE WHEN EXISTS (SELECT 1 FROM history.journey_call c"
                "   WHERE (c.feed, c.key_namespace, c.key, c.service_date, c.revision,"
                "          c.location_id, c.visit_n)"
                "       = (e.feed, e.key_namespace, e.key, e.service_date, %s,"
                "          e.location_id, e.visit_n)) THEN %s ELSE e.revision END"
                " WHERE (e.feed, e.key_namespace, e.key, e.service_date) = (%s, %s, %s, %s)",
                (revision, revision, revision, *key),
            )

    def _events(self, events: Sequence[WriteEvent]) -> None:
        if not events:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.actual_stop_event (feed, key_namespace, key, service_date,"
                " location_id, visit_n, event_type, revision, event_time, interval_lo,"
                " interval_hi, method, source, derivation_id)"
                " SELECT %s, %s, %s, %s, %s, %s, %s, j.latest_revision, %s, %s, %s, %s, %s, %s"
                " FROM history.journey j"
                " WHERE (j.feed, j.key_namespace, j.key, j.service_date) = (%s, %s, %s, %s)"
                " ON CONFLICT (feed, key_namespace, key, service_date, location_id, visit_n,"
                " event_type) DO UPDATE SET revision = excluded.revision,"
                " event_time = excluded.event_time, interval_lo = excluded.interval_lo,"
                " interval_hi = excluded.interval_hi, method = excluded.method,"
                " source = excluded.source, orphaned = false,"
                " derivation_id = excluded.derivation_id",
                [
                    (
                        *_key(e.journey),
                        e.location_id,
                        e.visit_n,
                        e.kind,
                        e.interval.lo + (e.interval.hi - e.interval.lo) / 2,
                        e.interval.lo,
                        e.interval.hi,
                        e.method,
                        e.source,
                        self.derivation_id,
                        *_key(e.journey),
                    )
                    for e in events
                ],
            )

    def _assignments(
        self, results: Sequence[ObservationResult], assigned: Sequence[AssignVehicle]
    ) -> None:
        seen: dict[tuple[str, str, JourneyKey], tuple[datetime, datetime]] = {}
        for result in results:
            vehicle = result.observation.first(VehicleKey)
            if result.journey is None or vehicle is None:
                continue
            key = (result.observation.source, vehicle.source_vehicle_id, result.journey)
            at = result.observation.at
            lo, hi = seen.get(key, (at, at))
            seen[key] = (min(lo, at), max(hi, at))
        for assignment in assigned:
            key = (
                assignment.vehicle.source,
                assignment.vehicle.source_vehicle_id,
                assignment.journey,
            )
            lo, hi = seen.get(key, (assignment.at, assignment.at))
            seen[key] = (min(lo, assignment.at), max(hi, assignment.at))
        if not seen:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.vehicle_assignment (feed, source, source_vehicle_id,"
                " key_namespace, key, service_date, first_seen, last_seen, method,"
                " derivation_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'keyed', %s)"
                " ON CONFLICT (feed, source, source_vehicle_id, key_namespace, key,"
                " service_date) DO UPDATE SET"
                " first_seen = least(history.vehicle_assignment.first_seen, excluded.first_seen),"
                " last_seen = greatest(history.vehicle_assignment.last_seen, excluded.last_seen)",
                [
                    (
                        journey.feed,
                        source,
                        vehicle,
                        journey.namespace,
                        journey.key,
                        journey.service_date,
                        lo,
                        hi,
                        self.derivation_id,
                    )
                    for (source, vehicle, journey), (lo, hi) in sorted(
                        seen.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])
                    )
                ],
            )

    # --- current state and rebuilds -------------------------------------------------------------

    def write_state(self, states: Mapping[Feed, FeedState]) -> None:
        release_id = self.derivation.release_id
        with self.connection.transaction():
            for feed, state in sorted(states.items()):
                self.connection.execute(
                    "DELETE FROM rt.vehicle_state_current WHERE feed = %s", (feed,)
                )
                self.connection.execute(
                    "DELETE FROM rt.trip_state_current WHERE feed = %s", (feed,)
                )
                with self.connection.cursor().copy(
                    "COPY rt.vehicle_state_current (feed, source, source_vehicle_id, status,"
                    " last_seen, key_namespace, key, service_date, trip_id, latitude, longitude,"
                    " bearing, reason, release_id) FROM STDIN"
                ) as copy:
                    for vehicle, v in sorted(state.vehicles.items()):
                        b = v.binding
                        copy.write_row(
                            (
                                feed,
                                vehicle.source,
                                vehicle.source_vehicle_id,
                                v.status,
                                v.last_seen,
                                None if b is None else b.journey.namespace,
                                None if b is None else b.journey.key,
                                None if b is None else b.journey.service_date,
                                None if b is None else b.trip_id,
                                None if v.position is None else v.position.lat,
                                None if v.position is None else v.position.lon,
                                None if v.position is None else v.position.bearing,
                                None if v.reason is None else v.reason.value,
                                release_id,
                            )
                        )
                with self.connection.cursor().copy(
                    "COPY rt.trip_state_current (feed, key_namespace, key, service_date,"
                    " release_id, trip_id, lifecycle, delay_s, off_route, stale, updated_at,"
                    " calls) FROM STDIN"
                ) as copy:
                    for journey, i in sorted(state.instances.items()):
                        copy.write_row(
                            (
                                *_key(journey),
                                i.release_id,
                                i.trip_id,
                                i.lifecycle,
                                i.delay_s,
                                i.off_route,
                                i.freshness.stale,
                                i.freshness.updated_at,
                                json.dumps([_call_json(c) for c in i.calls]),
                            )
                        )

    def clear_history(self, days: Iterable[date]) -> None:
        """Delete the history of service dates before a rebuild by replay."""

        chosen = sorted(set(days))
        if not chosen:
            return
        with self.connection.transaction():
            for table in HISTORY_TABLES:
                self.connection.execute(
                    sql.SQL("DELETE FROM history.{} WHERE service_date = ANY(%s)").format(
                        sql.Identifier(table)
                    ),
                    (chosen,),
                )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _call_json(call: CallState) -> dict[str, Any]:
    return {
        "sequence": call.sequence,
        "location_id": call.location_id,
        "visit_n": call.visit_n,
        "status": call.status,
        "estimated_arrival": _iso(call.estimated_arrival),
        "estimated_departure": _iso(call.estimated_departure),
        "source_class": call.source_class,
    }


def clear_observations(
    connection: psycopg.Connection, start: datetime, end: datetime, sources: Sequence[str]
) -> None:
    """Delete stored observations of `sources` received in [start, end) before a replay."""

    connection.execute(
        "DELETE FROM rt.observation WHERE received_at >= %s AND received_at < %s"
        " AND source = ANY(%s)",
        (start, end, list(sources)),
    )


def load_observations(
    connection: psycopg.Connection, since: datetime, until: datetime
) -> list[Observation]:
    """Stored observations received in [since, until), in processing order (warm replay)."""

    rows = connection.execute(
        "SELECT source, channel, feed, received_at, observed_at, raw_sha256, raw_item,"
        " decoder_version, facts FROM rt.observation"
        " WHERE received_at >= %s AND received_at < %s"
        " ORDER BY received_at, source, raw_sha256, raw_item",
        (since, until),
    ).fetchall()
    observations: list[Observation] = []
    for source, channel, feed, received_at, observed_at, sha256, item, version, facts in rows:
        observations.append(
            Observation(
                source=source,
                channel=channel,
                feed=feed,
                received_at=instant(received_at),
                observed_at=None if observed_at is None else instant(observed_at),
                raw=RawRef(sha256, item),
                decoder_version=version,
                facts=facts_from_json(facts),
            )
        )
    return observations
