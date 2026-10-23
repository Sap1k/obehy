"""Execute core effects against PostgreSQL: `rt.observation`, history, current state.

Partitions are created on demand: `rt.observation` per UTC hour, history per service date
(`emit/storage.py`). Each `write` is one transaction. History rows carry the writer's derivation, so
`clear_history` followed by a replay rebuilds days after a fix (BASE_PLAN.md section 29).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, LiteralString, cast

import psycopg
from psycopg import sql

from obehy.realtime.emit.represent import representing
from obehy.realtime.emit.storage import (
    HISTORY_TABLES,
    Terms,
    create_history_partitions,
    create_observation_partition,
    hour_of,
)
from obehy.realtime.emit.vehicles import VehicleBook
from obehy.realtime.model import (
    AssignVehicle,
    CallState,
    Derivation,
    Effect,
    Feed,
    FeedState,
    Instance,
    JourneyKey,
    LinkJourneys,
    ObservationResult,
    PartSpan,
    RecordCancellation,
    RecordPlatform,
    SnapshotJourney,
    VehicleId,
    VehicleKey,
    VehicleState,
    WriteEvent,
)
from obehy.realtime.model_json import facts_to_json
from obehy.realtime.runs import current_part, published_parts
from obehy.realtime.times import local_date


def _key(journey: JourneyKey) -> tuple[str, str, str, date]:
    return journey.feed, journey.namespace, journey.key, journey.service_date


@dataclass(slots=True)
class Writer:
    connection: psycopg.Connection
    derivation: Derivation
    derivation_id: int = 0
    _observation_hours: set[datetime] = field(default_factory=set[datetime])
    _history_days: set[date] = field(default_factory=set[date])
    terms: Terms = field(init=False)
    # Per feed, the current-state rows last written (a feed not in it is rewritten in full)
    # and each journey's calls JSON with the calls tuple it was made from.
    _state: dict[Feed, _Written] = field(default_factory=dict[Feed, "_Written"])
    # Which vehicle each report is, live attributes and profiles (emit/vehicles.py).
    vehicles: VehicleBook = field(init=False)

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
        self.terms = Terms.load(self.connection)
        self.vehicles = VehicleBook.load(self.connection)

    def reload_registry(self) -> None:
        """`obehy ref import` loaded a dataset: identities and profiles are redone."""

        self.vehicles.reload(self.connection)

    # --- partitions ---------------------------------------------------------------------------

    def _observation_partition(self, hour: datetime) -> None:
        if hour not in self._observation_hours:
            create_observation_partition(self.connection, hour)
            self._observation_hours.add(hour)

    def _history_partition(self, day: date) -> None:
        if day not in self._history_days:
            create_history_partitions(self.connection, day)
            self._history_days.add(day)

    # --- effects -------------------------------------------------------------------------------

    def write(self, effects: Sequence[Effect]) -> None:
        results = [e for e in effects if isinstance(e, ObservationResult)]
        snapshots = [e for e in effects if isinstance(e, SnapshotJourney)]
        events = [e for e in effects if isinstance(e, WriteEvent)]
        assigned = [e for e in effects if isinstance(e, AssignVehicle)]
        links = [e for e in effects if isinstance(e, LinkJourneys)]
        platforms = [e for e in effects if isinstance(e, RecordPlatform)]
        cancellations = [e for e in effects if isinstance(e, RecordCancellation)]
        for hour in {hour_of(r.observation.received_at) for r in results}:
            self._observation_partition(hour)
        for day in {e.journey.service_date for e in (*snapshots, *platforms, *cancellations)}:
            self._history_partition(day)
        self.terms.add(
            self.connection,
            [
                *(v for s in snapshots for c in s.calls for v in (c.location_id, c.name)),
                *(v for e in events for v in (e.location_id, e.method, e.source)),
            ],
        )
        with self.connection.transaction():
            self._observations(results)
            for snapshot in snapshots:
                self._snapshot(snapshot)
            self._events(events)
            self._assignments(results, assigned)
            self._links(links)
            self._platforms(platforms)
            self._cancellations(cancellations)
            self.vehicles.write(self.connection, self.vehicles.observe(results))

    def _observations(self, results: Sequence[ObservationResult]) -> None:
        if not results:
            return
        with self.connection.cursor().copy(
            "COPY rt.observation (received_at, source, channel, feed, raw_sha256, raw_item,"
            " observed_at, decoder_version, fact_schema_version, facts, journey_namespace,"
            " journey_key, service_date, reason, release_id, vehicle_id, state_since) FROM STDIN"
        ) as copy:
            for result in results:
                obs = result.observation
                facts = facts_to_json(obs.facts)
                journey = result.journey
                vehicle = obs.first(VehicleKey)
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
                        None if vehicle is None else vehicle.source_vehicle_id,
                        # A row without a vehicle (a board row) starts its own state.
                        result.state_since or obs.received_at,
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
            " revision, release_id, trip_id, route_name, headsign, derivation_id, run_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                *key,
                revision,
                snapshot.release_id,
                snapshot.trip_id,
                snapshot.route_name,
                snapshot.headsign,
                self.derivation_id,
                snapshot.run_key,
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

    def _events(self, events: Sequence[WriteEvent]) -> None:
        if not events:
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
                        self.terms[e.location_id],
                        e.visit_n,
                        e.kind,
                        e.interval.lo + (e.interval.hi - e.interval.lo) / 2,
                        e.interval.lo,
                        e.interval.hi,
                        self.terms[e.method],
                        self.terms[e.source],
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
            at = result.observation.at
            # History is keyed by public journeys (rail: train numbers), not by the run.
            for journey in result.public or (result.journey,):
                key = (result.observation.source, vehicle.source_vehicle_id, journey)
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

    def _links(self, links: Sequence[LinkJourneys]) -> None:
        if not links:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO history.journey_link (feed, source_namespace, source_key,"
                " target_namespace, target_key, service_date, kind, derivation_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    (
                        link.source.feed,
                        link.source.namespace,
                        link.source.key,
                        link.target.namespace,
                        link.target.key,
                        link.source.service_date,
                        link.kind,
                        self.derivation_id,
                    )
                    for link in links
                ],
            )

    def _cancellations(self, cancellations: Sequence[RecordCancellation]) -> None:
        """A cancellation is kept per journey and source (first and last said); a vehicle seen
        running the journey after all restores it, whichever source cancelled it, until a
        later cancellation."""

        for c in cancellations:
            if c.cancelled:
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
                    (*_key(c.journey), c.source, c.at, c.at, self.derivation_id),
                )
            else:
                self.connection.execute(
                    "UPDATE history.journey_cancellation SET restored_at = %s"
                    " WHERE (feed, key_namespace, key, service_date) = (%s, %s, %s, %s)"
                    " AND restored_at IS NULL AND last_seen <= %s",
                    (c.at, *_key(c.journey), c.at),
                )

    def _platforms(self, platforms: Sequence[RecordPlatform]) -> None:
        if not platforms:
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
                [
                    (
                        *_key(e.journey),
                        e.location_id,
                        e.visit_n,
                        e.kind,
                        e.platform.value,
                        e.platform.label,
                        e.platform.boarding_point_id,
                        e.platform.source,
                        e.platform.assigned_at,
                        e.platform.assigned_at,
                        self.derivation_id,
                    )
                    for e in platforms
                ],
            )

    # --- current state and rebuilds -------------------------------------------------------------

    def write_state(self, states: Mapping[Feed, FeedState]) -> None:
        """Bring the current-state tables in line with `states`.

        A feed's first write replaces its rows; later writes upsert the rows that changed since
        and delete the ones that went, so a tick costs what changed, not the whole state."""

        release_id = self.derivation.release_id
        written: dict[Feed, _Written] = {}
        self.vehicles.track(states)  # pairings first: they may change who a report is
        try:
            with self.connection.transaction():
                for feed, state in sorted(states.items()):
                    before = self._state.get(feed)
                    shown = representing(state)
                    now = _Written(
                        {
                            (feed, v.source, v.source_vehicle_id): _vehicle_row(
                                feed, v, current, state, self.vehicles, release_id, v in shown
                            )
                            for v, current in state.vehicles.items()
                        },
                        {},
                        {},
                        {},
                    )
                    for journey, instance in state.instances.items():
                        calls = None if before is None else before.calls.get(journey)
                        if calls is None or calls[0] is not instance.calls:
                            encoded = json.dumps([_call_json(c) for c in instance.calls])
                            calls = (instance.calls, encoded)
                        now.calls[journey] = calls
                        now.trips[_key(journey)] = _trip_row(instance, calls[1])
                        for part in published_parts(instance):
                            row = _part_row(instance, part, release_id)
                            now.parts[(*_key(journey), part.trip_id)] = row
                    self._write_table(feed, _VEHICLES, now.vehicles, before and before.vehicles)
                    self._write_table(feed, _TRIPS, now.trips, before and before.trips)
                    self._write_table(feed, _PARTS, now.parts, before and before.parts)
                    written[feed] = now
                # Profiles of vehicles whose identity changed with these rows.
                self.vehicles.write(self.connection, [])
        except BaseException:
            self._state.clear()  # unknown what was written: rewrite in full next time
            raise
        self._state.update(written)

    def _write_table(self, feed: Feed, table: _Table, rows: _Rows, old: _Rows | None) -> None:
        columns = sql.SQL(", ").join(sql.Identifier(c) for c in table.columns)
        if old is None:
            self.connection.execute(
                sql.SQL("DELETE FROM rt.{} WHERE feed = %s").format(table.name), (feed,)
            )
            with self.connection.cursor().copy(
                sql.SQL("COPY rt.{} ({}) FROM STDIN").format(table.name, columns)
            ) as copy:
                for key in sorted(rows):
                    copy.write_row(rows[key])
            return
        gone = sorted(old.keys() - rows.keys())
        if gone:
            self.connection.execute(
                sql.SQL("DELETE FROM rt.{} WHERE ({}) IN (SELECT * FROM unnest({}))").format(
                    table.name,
                    sql.SQL(", ").join(sql.Identifier(c) for c in table.key),
                    sql.SQL(", ").join(
                        sql.SQL("%s::{}[]").format(sql.SQL(t)) for t in table.key_types
                    ),
                ),
                [list(column) for column in zip(*gone, strict=True)],
            )
        changed = [rows[key] for key in sorted(rows) if old.get(key) != rows[key]]
        if not changed:
            return
        stage = sql.Identifier(f"{table.name_text}_stage")
        self.connection.execute(
            sql.SQL("CREATE TEMP TABLE IF NOT EXISTS {} (LIKE rt.{}) ON COMMIT DELETE ROWS").format(
                stage, table.name
            )
        )
        with self.connection.cursor().copy(
            sql.SQL("COPY {} ({}) FROM STDIN").format(stage, columns)
        ) as copy:
            for row in changed:
                copy.write_row(row)
        updates = [c for c in table.columns if c not in table.key]
        self.connection.execute(
            sql.SQL(
                "INSERT INTO rt.{} ({}) SELECT {} FROM {}"
                " ON CONFLICT ({}) DO UPDATE SET ({}) = ({})"
            ).format(
                table.name,
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


_Rows = dict[tuple[Any, ...], tuple[Any, ...]]


@dataclass(frozen=True, slots=True)
class _Table:
    name_text: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    key_types: tuple[LiteralString, ...]

    @property
    def name(self) -> sql.Identifier:
        return sql.Identifier(self.name_text)


_VEHICLES = _Table(
    "vehicle_state_current",
    (
        "feed",
        "source",
        "source_vehicle_id",
        "status",
        "last_seen",
        "key_namespace",
        "key",
        "service_date",
        "trip_id",
        "latitude",
        "longitude",
        "bearing",
        "reason",
        "release_id",
        "vehicle_id",
        "bound_by",
        "represents",
    ),
    ("feed", "source", "source_vehicle_id"),
    ("text", "text", "text"),
)
_TRIPS = _Table(
    "trip_state_current",
    (
        "feed",
        "key_namespace",
        "key",
        "service_date",
        "release_id",
        "trip_id",
        "lifecycle",
        "delay_s",
        "off_route",
        "stale",
        "updated_at",
        "calls",
        "sources",
    ),
    ("feed", "key_namespace", "key", "service_date"),
    ("text", "text", "text", "date"),
)
# The feed trips each journey publishes over its calls (a run's parts, a road trip itself), so
# readers find a trip's state by its feed trip ID (BASE_PLAN.md section 30).
_PARTS = _Table(
    "trip_part_current",
    (
        "feed",
        "key_namespace",
        "key",
        "service_date",
        "trip_id",
        "first_call",
        "last_call",
        "release_id",
    ),
    ("feed", "key_namespace", "key", "service_date", "trip_id"),
    ("text", "text", "text", "date", "text"),
)


@dataclass(slots=True)
class _Written:
    vehicles: _Rows
    trips: _Rows
    parts: _Rows
    calls: dict[JourneyKey, tuple[tuple[CallState, ...], str]]


def _vehicle_row(
    feed: Feed,
    vehicle: VehicleId,
    v: VehicleState,
    state: FeedState,
    book: VehicleBook,
    release_id: str,
    represents: bool,
) -> tuple[Any, ...]:
    """The vehicle's trip is the feed trip it is on: on a rail run, the part it is driving.
    `represents`: it stands for its journey on the map (`emit/represent.py`)."""

    b = v.binding
    instance = None if b is None else state.instances.get(b.journey)
    trip_id = None if b is None else b.trip_id if instance is None else current_part(instance)
    return (
        feed,
        vehicle.source,
        vehicle.source_vehicle_id,
        v.status,
        v.last_seen,
        None if b is None else b.journey.namespace,
        None if b is None else b.journey.key,
        None if b is None else b.journey.service_date,
        trip_id,
        None if v.position is None else v.position.lat,
        None if v.position is None else v.position.lon,
        None if v.position is None else v.position.bearing,
        None if v.reason is None else v.reason.value,
        release_id,
        *book.identify((vehicle.source, vehicle.source_vehicle_id), local_date(v.last_seen)),
        represents,
    )


def _part_row(i: Instance, part: PartSpan, release_id: str) -> tuple[Any, ...]:
    return (*_key(i.journey), part.trip_id, part.first, part.last, release_id)


def _trip_row(i: Instance, calls: str) -> tuple[Any, ...]:
    return (
        *_key(i.journey),
        i.release_id,
        i.trip_id,
        i.lifecycle,
        i.delay_s,
        i.off_route,
        i.freshness.stale,
        i.freshness.updated_at,
        calls,
        json.dumps(_sources_json(i)),
    )


def _sources_json(i: Instance) -> list[dict[str, Any]]:
    """Which sources the journey's realtime came from (the API's provenance), by source."""

    return [
        {
            "source": t.source,
            "heard_at": _iso(t.heard_at),
            "position_at": _iso(t.position_at),
            "delay_s": t.delay_s,
            "leads_position": t.source == i.track_source,
        }
        for t in sorted(i.sources, key=lambda t: t.source)
    ]


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
        "passenger": call.passenger,
        "platform": None
        if call.platform is None
        else {
            "value": call.platform.value,
            "label": call.platform.label,
            "boarding_point_id": call.platform.boarding_point_id,
            "source": call.platform.source,
        },
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
