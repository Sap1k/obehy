"""Core effects and state as rows (`emit/rows.py`): `rt.observation`, history, current state.

`Writer` turns effects into rows and keeps the current-state tables in step with core state; its
`Store` writes them (`emit/sql_store.py`: PostgreSQL). Each `write` is one transaction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from obehy.realtime.emit.represent import representing, shown
from obehy.realtime.emit.rows import PARTS, TRIPS, VEHICLES, Row, StateTable, Store
from obehy.realtime.emit.storage import hour_of
from obehy.realtime.emit.vehicles import VehicleBook
from obehy.realtime.facts import Feed, VehicleKey
from obehy.realtime.model import (
    AssignVehicle,
    CallState,
    Derivation,
    Effect,
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
    store: Store
    derivation: Derivation
    # Per feed, the current-state rows last written (a feed not in it is rewritten in full)
    # and each journey's calls JSON with the calls tuple it was made from.
    _state: dict[Feed, _Written] = field(default_factory=dict[Feed, "_Written"])
    # Which vehicle each report is, live attributes and profiles (emit/vehicles.py).
    vehicles: VehicleBook = field(init=False)

    def __post_init__(self) -> None:
        self.vehicles = self.store.open(self.derivation)

    def reload_registry(self) -> None:
        """`obehy ref import` loaded a dataset: identities and profiles are redone."""

        self.store.reload_registry(self.vehicles)

    # --- effects -------------------------------------------------------------------------------

    def write(self, effects: Sequence[Effect]) -> None:
        results = [e for e in effects if isinstance(e, ObservationResult)]
        snapshots = [e for e in effects if isinstance(e, SnapshotJourney)]
        events = [e for e in effects if isinstance(e, WriteEvent)]
        assigned = [e for e in effects if isinstance(e, AssignVehicle)]
        links = [e for e in effects if isinstance(e, LinkJourneys)]
        platforms = [e for e in effects if isinstance(e, RecordPlatform)]
        cancellations = [e for e in effects if isinstance(e, RecordCancellation)]
        store = self.store
        store.prepare(
            {hour_of(r.observation.received_at) for r in results},
            {e.journey.service_date for e in (*snapshots, *platforms, *cancellations)},
            [
                *(v for s in snapshots for c in s.calls for v in (c.location_id, c.name)),
                *(v for e in events for v in (e.location_id, e.method, e.source)),
            ],
        )
        with store.transaction():
            store.observations([observation_row(r, self.derivation.release_id) for r in results])
            for snapshot in snapshots:
                store.snapshot(snapshot)
            store.events([_event(e) for e in events])
            # Live attributes first: what a report states may name its vehicle.
            attributes = self.vehicles.observe(results)
            store.assignments(self._assignments(results, assigned))
            store.links([_link(link) for link in links])
            store.platforms([_platform(e) for e in platforms])
            store.cancellations(
                [(*_key(c.journey), c.source, c.at, c.cancelled) for c in cancellations]
            )
            store.vehicles(self.vehicles.rows(attributes))

    def _assignments(
        self, results: Sequence[ObservationResult], assigned: Sequence[AssignVehicle]
    ) -> list[Row]:
        seen: dict[tuple[str, str, JourneyKey], tuple[datetime, datetime, str]] = {}

        def note(key: tuple[str, str, JourneyKey], at: datetime, method: str) -> None:
            lo, hi, first = seen.get(key, (at, at, method))
            seen[key] = (min(lo, at), max(hi, at), first)

        for result in results:
            vehicle = result.observation.first(VehicleKey)
            if result.journey is None or vehicle is None:
                continue
            # History is keyed by public journeys (rail: train numbers), not by the run.
            for journey in result.public or (result.journey,):
                key = (result.observation.source, vehicle.source_vehicle_id, journey)
                note(key, result.observation.at, result.method)
        for assignment in assigned:
            vehicle_id = assignment.vehicle
            key = (vehicle_id.source, vehicle_id.source_vehicle_id, assignment.journey)
            note(key, assignment.at, assignment.method)
        return [
            (
                journey.feed,
                source,
                vehicle,
                journey.namespace,
                journey.key,
                journey.service_date,
                lo,
                hi,
                method,
                self.vehicles.worked_by((source, vehicle), journey.service_date),
            )
            for (source, vehicle, journey), (lo, hi, method) in sorted(
                seen.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])
            )
        ]

    # --- current state and rebuilds -------------------------------------------------------------

    def write_state(self, states: Mapping[Feed, FeedState]) -> None:
        """Bring the current-state tables in line with `states`.

        A feed's first write replaces its rows; later writes upsert the rows that changed since
        and delete the ones that went, so a tick costs what changed, not the whole state."""

        release_id = self.derivation.release_id
        written: dict[Feed, _Written] = {}
        self.vehicles.track(states)  # pairings first: they may change who a report is
        try:
            with self.store.transaction():
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
                            left = instance.left_index()
                            encoded = json.dumps(
                                [_call_json(c, i <= left) for i, c in enumerate(instance.calls)]
                            )
                            calls = (instance.calls, encoded)
                        now.calls[journey] = calls
                        now.trips[_key(journey)] = _trip_row(instance, calls[1])
                        for part in published_parts(instance):
                            row = _part_row(instance, part, release_id)
                            now.parts[(*_key(journey), part.trip_id)] = row
                    self._write_table(feed, VEHICLES, now.vehicles, before and before.vehicles)
                    self._write_table(feed, TRIPS, now.trips, before and before.trips)
                    self._write_table(feed, PARTS, now.parts, before and before.parts)
                    written[feed] = now
                # Profiles of vehicles whose identity changed with these rows.
                self.store.vehicles(self.vehicles.rows([]))
        except BaseException:
            self._state.clear()  # unknown what was written: rewrite in full next time
            raise
        self._state.update(written)

    def _write_table(self, feed: Feed, table: StateTable, rows: _Rows, old: _Rows | None) -> None:
        if old is None:
            self.store.replace_state(table, feed, [rows[key] for key in sorted(rows)])
            return
        gone = sorted(old.keys() - rows.keys())
        if gone:
            self.store.delete_state(table, gone)
        changed = [rows[key] for key in sorted(rows) if old.get(key) != rows[key]]
        if changed:
            self.store.upsert_state(table, changed)

    def clear_history(self, days: Iterable[date]) -> None:
        """Delete the history of service dates before a rebuild by replay."""

        self.store.clear_history(sorted(set(days)))


def observation_row(result: ObservationResult, release_id: str) -> Row:
    """The `rt.observation` row of an observation's result (`OBSERVATION_COLUMNS`)."""

    obs = result.observation
    facts = facts_to_json(obs.facts)
    journey = result.journey
    vehicle = obs.first(VehicleKey)
    return (
        obs.received_at,
        obs.source,
        obs.channel,
        obs.feed,
        obs.raw.sha256,
        obs.raw.item,
        obs.observed_at,
        obs.decoder_version,
        facts["v"],
        facts,
        None if journey is None else journey.namespace,
        None if journey is None else journey.key,
        None if journey is None else journey.service_date,
        None if result.reason is None else result.reason.value,
        release_id,
        None if vehicle is None else vehicle.source_vehicle_id,
        # A row without a vehicle (a board row) starts its own state.
        result.state_since or obs.received_at,
    )


def _event(e: WriteEvent) -> Row:
    return (
        *_key(e.journey),
        e.location_id,
        e.visit_n,
        e.kind,
        e.interval.mid,
        e.interval.lo,
        e.interval.hi,
        e.method,
        e.source,
    )


def _link(link: LinkJourneys) -> Row:
    return (
        link.source.feed,
        link.source.namespace,
        link.source.key,
        link.target.namespace,
        link.target.key,
        link.source.service_date,
        link.kind,
    )


def _platform(e: RecordPlatform) -> Row:
    return (
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
    )


_Rows = dict[tuple[Any, ...], tuple[Any, ...]]


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
    `represents`: it stands for its journey on the map (`emit/represent.py`), drawn where the
    journey's tracker reads it from every source's fixes."""

    b = v.binding
    instance = None if b is None else state.instances.get(b.journey)
    trip_id = None if b is None else b.trip_id if instance is None else current_part(instance)
    lat, lon, bearing, position_at = (
        (None, None, None, v.position_at)
        if v.position is None
        else (v.position.lat, v.position.lon, v.position.bearing, v.position_at)
    )
    drawn = shown(instance, vehicle) if represents else None
    if drawn is not None:
        lat, lon, bearing, position_at = drawn.lat, drawn.lon, drawn.bearing, drawn.at
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
        lat,
        lon,
        bearing,
        None if v.reason is None else v.reason.value,
        release_id,
        *book.identify((vehicle.source, vehicle.source_vehicle_id), local_date(v.last_seen)),
        represents,
        position_at,
    )


def _part_row(i: Instance, part: PartSpan, release_id: str) -> tuple[Any, ...]:
    return (*_key(i.journey), part.trip_id, part.first, part.last, release_id)


def _trip_row(i: Instance, calls: str) -> tuple[Any, ...]:
    return (
        *_key(i.journey),
        i.release_id,
        i.trip_id,
        i.lifecycle,
        i.lateness_s,
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


def _call_json(call: CallState, passed: bool) -> dict[str, Any]:
    """A call's current state; `passed`: the vehicle has left it (`Instance.left_index`)."""

    return {
        "sequence": call.sequence,
        "location_id": call.location_id,
        "visit_n": call.visit_n,
        "status": call.status,
        "passed": passed,
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
