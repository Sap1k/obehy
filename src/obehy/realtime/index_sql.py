"""Fill an `Index` from PostgreSQL, lazily by source key (docs/R1_SLICE.md, ticket 3).

Queries read `static.*` by `load_id` rather than `active.*`, so replay can use a retained release
that is not the active one; the live worker passes the active publication's loads. This is the
one sanctioned reader outside `active.*` (AGENTS.md).

Rail trips load with every part of their run (PA), their train numbers and the `sr70` keys of
their locations (and the `sr70:track` keys of those stations' tracks), so the core can follow the
whole run and resolve SŽ points and tracks (docs/R2_SLICE.md sections 1 and 2).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg

from obehy.realtime.geo import cumulative_m
from obehy.realtime.index import Call, Index, KeyEntry, KeyRef, Location, Shape, Trip, parent_ref
from obehy.realtime.model import Feed
from obehy.release.contract import load_contract
from obehy.release.load import load_release


class IndexLoadError(RuntimeError):
    """The requested release or loads are not available in the database."""


@dataclass(frozen=True, slots=True)
class ReleaseLoads:
    run_id: str
    load_ids: dict[Feed, int]


def active_loads(connection: psycopg.Connection) -> ReleaseLoads:
    row = connection.execute(
        "SELECT run_id, jdf_load_id, czptt_load_id FROM control.publication"
    ).fetchone()
    if row is None or row[0] is None:
        raise IndexLoadError("no release is active")
    return ReleaseLoads(cast(str, row[0]), {"jdf": cast(int, row[1]), "czptt": cast(int, row[2])})


def run_loads(connection: psycopg.Connection, run_id: str) -> ReleaseLoads | None:
    """The newest loaded jdf and czptt loads of a run, or None if the run is not loaded."""

    rows = connection.execute(
        "SELECT package, max(load_id) FROM control.load"
        " WHERE run_id = %s AND status = 'loaded' GROUP BY package",
        (run_id,),
    ).fetchall()
    found = {cast(str, package): cast(int, load_id) for package, load_id in rows}
    if set(found) != {"jdf", "czptt"}:
        return None
    return ReleaseLoads(run_id, {"jdf": found["jdf"], "czptt": found["czptt"]})


def ensure_release(
    connection: psycopg.Connection, release: str | Path, report: Any = print
) -> ReleaseLoads:
    """The loads of a run, loading its release directory first if it is not in the database.

    `release` is a run ID already in the database or a release directory; no activation is
    needed, so replay can use a scratch or shared development database.
    """

    path = Path(release)
    run_id = (
        cast(str, json.loads((path / "release.json").read_text(encoding="utf-8"))["run_id"])
        if path.is_dir()
        else str(release)
    )
    loads = run_loads(connection, run_id)
    if loads is None:
        if not path.is_dir():
            raise IndexLoadError(f"release {run_id} is not loaded and is not a directory")
        load_release(connection, path, load_contract(), report=report)
        loads = run_loads(connection, run_id)
        if loads is None:
            raise IndexLoadError(f"release {run_id} did not load both packages")
    return loads


class IndexLoader:
    """Loads one feed of one release into an `Index`, a batch of keys at a time."""

    def __init__(self, connection: psycopg.Connection, loads: ReleaseLoads, feed: Feed) -> None:
        self.connection = connection
        self.load_id = loads.load_ids[feed]
        self.index = Index(loads.run_id, feed)

    def ensure(self, refs: Iterable[KeyRef], dates: Iterable[date]) -> Index:
        """Load every key in `refs` (and its parent) not seen yet, and service dates for
        `dates`; return the index."""

        wanted = set(refs)
        wanted |= {parent for ref in wanted if (parent := parent_ref(ref)) is not None}
        missing = self.index.missing(wanted)
        new_dates = set(dates) - self.index.loaded_dates
        if missing:
            self._load_keys(missing)
        if new_dates:
            self._load_dates(self.index.service_dates.keys(), new_dates, extend=True)
        return self.index

    def _load_keys(self, refs: set[KeyRef]) -> None:
        ordered = sorted(refs)
        found: dict[KeyRef, list[KeyEntry]] = defaultdict(list)
        kinds: dict[str, str] = {}
        for namespace, identifier, public_id, kind, valid_from, valid_to in self._rows_first(
            "SELECT k.namespace, k.identifier, k.public_id, k.entity_kind, k.valid_from,"
            " k.valid_to FROM static.source_key k"
            " JOIN unnest(%s::text[], %s::text[]) AS w (namespace, identifier)"
            " USING (namespace, identifier) WHERE k.load_id = %s"
            " ORDER BY 1, 2, 3, 5",
            [ref[0] for ref in ordered],
            [ref[1] for ref in ordered],
        ):
            found[(namespace, identifier)].append(KeyEntry(public_id, valid_from, valid_to))
            kinds[public_id] = kind
        for ref in ordered:
            self.index.key_entries[ref] = tuple(found.get(ref, ()))
        trips = sorted(
            public_id
            for public_id, kind in kinds.items()
            if kind == "trip" and public_id not in self.index.trips
        )
        if trips:
            self._load_trips(trips)

    def _rows_first(self, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
        # The load_id parameter goes last for queries that bind arrays first.
        return self.connection.execute(query, (*params, self.load_id)).fetchall()

    def _load_trips(self, trip_ids: list[str]) -> None:
        heads = self._rows_first(
            "SELECT t.trip_id, t.route_id, coalesce(r.short_name, r.route_id), r.mode,"
            " t.service_id, t.headsign, t.shape_id, t.run_key, t.run_part,"
            " (SELECT min(k.identifier) FROM static.source_key k"
            "  WHERE k.load_id = t.load_id AND k.public_id = t.trip_id"
            "  AND k.namespace = 'czptt:train_number')"
            " FROM static.trip t JOIN static.route r"
            " ON r.load_id = t.load_id AND r.route_id = t.route_id"
            " WHERE t.trip_id = ANY(%s) AND t.load_id = %s",
            trip_ids,
        )
        calls: dict[str, list[Call]] = defaultdict(list)
        locations: set[str] = set()
        for (
            trip_id,
            sequence,
            location_id,
            visit_n,
            passenger,
            arrival,
            departure,
        ) in self._rows_first(
            "SELECT trip_id, sequence, location_id,"
            " row_number() OVER (PARTITION BY trip_id, location_id ORDER BY sequence),"
            " passenger_service, scheduled_arrival, scheduled_departure"
            " FROM static.trip_call WHERE trip_id = ANY(%s) AND load_id = %s"
            " ORDER BY trip_id, sequence",
            trip_ids,
        ):
            calls[trip_id].append(
                Call(sequence, location_id, visit_n, passenger, arrival, departure)
            )
            locations.add(location_id)
        shapes: set[str] = set()
        services: set[str] = set()
        runs: set[str] = set()
        for (
            trip_id,
            route_id,
            route_name,
            mode,
            service_id,
            headsign,
            shape_id,
            run_key,
            run_part,
            train_number,
        ) in heads:
            self.index.trips[trip_id] = Trip(
                trip_id,
                route_id,
                route_name,
                mode,
                service_id,
                headsign,
                shape_id,
                tuple(calls[trip_id]),
                run_key=run_key,
                run_part=run_part,
                train_number=train_number,
            )
            if run_key is not None and run_key not in self.index.runs:
                runs.add(run_key)
            if shape_id is not None and shape_id not in self.index.shapes:
                shapes.add(shape_id)
            if service_id not in self.index.service_dates:
                services.add(service_id)
        new_locations = sorted(locations - self.index.locations.keys())
        self._load_locations(new_locations)
        if self.index.feed == "czptt":
            self._load_rail_keys(new_locations)
        if shapes:
            self._load_shapes(sorted(shapes))
        if services and self.index.loaded_dates:
            self._load_dates(services, set(self.index.loaded_dates), extend=False)
        for service_id in services:
            self.index.service_dates.setdefault(service_id, frozenset())
        if runs:
            self._load_runs(sorted(runs))

    def _load_runs(self, run_keys: list[str]) -> None:
        """Every part of each run, in `run_part` order; parts not loaded yet load like any
        trip."""

        parts: dict[str, list[str]] = defaultdict(list)
        for run_key, trip_id in self._rows_first(
            "SELECT run_key, trip_id FROM static.trip"
            " WHERE run_key = ANY(%s) AND load_id = %s ORDER BY run_key, run_part, trip_id",
            run_keys,
        ):
            parts[run_key].append(trip_id)
        for run_key in run_keys:
            self.index.runs[run_key] = tuple(parts.get(run_key, ()))
        siblings = sorted({t for ids in parts.values() for t in ids if t not in self.index.trips})
        if siblings:
            self._load_trips(siblings)

    def _load_rail_keys(self, location_ids: list[str]) -> None:
        """`sr70` keys of the locations, and the `sr70:track` keys of those stations' tracks."""

        if not location_ids:
            return
        codes: set[str] = set()
        for identifier, public_id in self._rows_first(
            "SELECT identifier, public_id FROM static.source_key"
            " WHERE entity_kind = 'location' AND namespace = 'sr70'"
            " AND public_id = ANY(%s) AND load_id = %s ORDER BY 1, 2",
            location_ids,
        ):
            self.index.add_location_key(public_id, "sr70", identifier)
            codes.add(identifier)
        if not codes:
            return
        for identifier, public_id in self._rows_first(
            "SELECT identifier, public_id FROM static.source_key"
            " WHERE entity_kind = 'location' AND namespace = 'sr70:track'"
            " AND split_part(identifier, ':', 1) = ANY(%s) AND load_id = %s ORDER BY 1, 2",
            sorted(codes),
        ):
            self.index.add_location_key(public_id, "sr70:track", identifier)

    def _load_locations(self, location_ids: list[str]) -> None:
        if not location_ids:
            return
        for location_id, name, lon, lat in self._rows_first(
            "SELECT location_id, name, longitude, latitude FROM static.location"
            " WHERE location_id = ANY(%s) AND load_id = %s",
            location_ids,
        ):
            self.index.locations[location_id] = Location(location_id, name, lon, lat)

    def _load_shapes(self, shape_ids: list[str]) -> None:
        points: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for shape_id, lon, lat in self._rows_first(
            "SELECT shape_id, longitude, latitude FROM static.shape_point"
            " WHERE shape_id = ANY(%s) AND load_id = %s ORDER BY shape_id, sequence",
            shape_ids,
        ):
            points[shape_id].append((lon, lat))
        for shape_id in shape_ids:
            vertices = tuple(points.get(shape_id, ()))
            # Distances come from the geometry; feed `distance_traveled` units vary.
            self.index.shapes[shape_id] = Shape(shape_id, vertices, cumulative_m(vertices))

    def _load_dates(self, service_ids: Iterable[str], days: set[date], *, extend: bool) -> None:
        ids = sorted(service_ids)
        running: dict[str, set[date]] = defaultdict(set)
        if ids:
            for service_id, day in self._rows_first(
                "SELECT service_id, service_date FROM static.service_date"
                " WHERE service_id = ANY(%s) AND service_date = ANY(%s) AND load_id = %s",
                ids,
                sorted(days),
            ):
                running[service_id].add(day)
        for service_id in ids:
            self.index.service_dates[service_id] = self.index.service_dates.get(
                service_id, frozenset()
            ) | frozenset(running.get(service_id, ()))
        if extend:
            self.index.loaded_dates = self.index.loaded_dates | frozenset(days)
