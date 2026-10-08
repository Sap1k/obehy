"""Read-only lookup indexes over one package of a Parquet release directory.

Only the source-key namespaces a replay needs are loaded, plus the trips, services and scheduled
spans they reference. Scheduled times are seconds after the operating day's midnight.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from obehy.production_package import ProductionPackageError, read_manifest


def _read(
    path: Path, columns: list[str] | None = None, filters: list[Any] | None = None
) -> pa.Table:
    return pq.read_table(path, columns=columns, filters=filters)  # pyright: ignore[reportUnknownMemberType]


def _keep(table: pa.Table, column: str, values: Collection[str]) -> pa.Table:
    """Rows of `table` whose `column` is in `values`."""

    value_set = pa.array(sorted(values), type=pa.string())
    return table.filter(pc.is_in(table.column(column), value_set))  # pyright: ignore[reportUnknownMemberType]


def _rows(table: pa.Table, columns: Sequence[str]) -> Iterator[tuple[Any, ...]]:
    return zip(*(table.column(name).to_pylist() for name in columns), strict=True)


class ReleaseIndexError(RuntimeError):
    """A release package cannot be indexed."""


@dataclass(frozen=True, slots=True)
class KeyEntry:
    public_id: str
    valid_from: date
    valid_to: date

    def valid_on(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass(frozen=True, slots=True)
class TripInfo:
    service_id: str
    run_key: str | None


@dataclass(frozen=True, slots=True)
class Call:
    sequence: int
    time: int | None
    stop_name: str


class PackageIndex:
    def __init__(self, root: Path, namespaces: Collection[str]) -> None:
        try:
            manifest = read_manifest(root)
        except ProductionPackageError as error:
            raise ReleaseIndexError(f"{root}: {error}") from error
        self.root = root
        self.feed_version = cast(str | None, manifest.get("feed_version"))
        self._paths = {
            cast(str, relation["name"]): root / cast(str, relation["path"])
            for relation in cast(list[dict[str, Any]], manifest.get("relations", []))
        }
        self._keys: dict[tuple[str, str], list[KeyEntry]] = defaultdict(list)
        self._by_prefix: dict[tuple[str, str], list[KeyEntry]] | None = None
        self._trips: dict[str, TripInfo] = {}
        self._calendars: dict[str, list[tuple[date, date, int]]] = defaultdict(list)
        self._exceptions: dict[tuple[str, date], bool] = {}
        self._spans: dict[str, tuple[int, int]] = {}
        self._run_spans: dict[str, tuple[int, int]] = {}
        self._calls: dict[str, list[Call]] = {}
        self._load(sorted(namespaces))

    def _path(self, relation: str) -> Path:
        path = self._paths.get(relation)
        if path is None or not path.is_file():
            raise ReleaseIndexError(f"{self.root}: relation {relation} is missing")
        return path

    def _load(self, namespaces: Sequence[str]) -> None:
        keys = _read(
            self._path("source_key"),
            ["namespace", "identifier", "public_id", "valid_from", "valid_to"],
            [("namespace", "in", list(namespaces))],
        )
        for namespace, identifier, public_id, valid_from, valid_to in _rows(
            keys, keys.column_names
        ):
            self._keys[(namespace, identifier)].append(KeyEntry(public_id, valid_from, valid_to))
        trip_ids = {entry.public_id for entries in self._keys.values() for entry in entries}

        trips = _keep(
            _read(self._path("trip"), ["trip_id", "service_id", "run_key"]), "trip_id", trip_ids
        )
        for trip_id, service_id, run_key in _rows(trips, trips.column_names):
            self._trips[trip_id] = TripInfo(service_id, run_key)
        service_ids = {trip.service_id for trip in self._trips.values()}

        calendar = _keep(_read(self._path("service_calendar")), "service_id", service_ids)
        for service_id, valid_from, valid_to, mask in _rows(
            calendar, ("service_id", "valid_from", "valid_to", "weekday_mask")
        ):
            self._calendars[service_id].append((valid_from, valid_to, mask))
        exceptions = _keep(_read(self._path("service_exception")), "service_id", service_ids)
        for service_id, service_date, added in _rows(
            exceptions, ("service_id", "service_date", "added")
        ):
            key = (service_id, service_date)
            self._exceptions[key] = self._exceptions.get(key, False) or bool(added)

        calls = _keep(
            _read(self._path("trip_call"), ["trip_id", "scheduled_arrival", "scheduled_departure"]),
            "trip_id",
            trip_ids,
        )
        arrival = calls.column("scheduled_arrival")
        departure = calls.column("scheduled_departure")
        columns: dict[str, Any] = {
            "trip_id": calls.column("trip_id"),
            "first": pc.coalesce(departure, arrival),
            "last": pc.coalesce(arrival, departure),
        }
        spans = (
            pa.table(columns)  # pyright: ignore[reportUnknownMemberType]
            .group_by("trip_id")
            .aggregate([("first", "min"), ("last", "max")])
        )
        for trip_id, first, last in _rows(spans, ("trip_id", "first_min", "last_max")):
            if first is None or last is None:
                continue
            self._spans[trip_id] = (first, last)
            run_key = self._trips[trip_id].run_key if trip_id in self._trips else None
            if run_key is not None:
                current = self._run_spans.get(run_key)
                self._run_spans[run_key] = (
                    (first, last)
                    if current is None
                    else (min(current[0], first), max(current[1], last))
                )

    def keys(self, namespace: str, identifier: str) -> Sequence[KeyEntry]:
        return self._keys.get((namespace, identifier), ())

    def keys_with_prefix(self, namespace: str, prefix: str) -> Sequence[KeyEntry]:
        """Keys whose identifier is `<prefix>:…`, e.g. every `cis:line_trip` key of a line."""

        if self._by_prefix is None:
            by_prefix: dict[tuple[str, str], list[KeyEntry]] = defaultdict(list)
            for (key_namespace, identifier), entries in self._keys.items():
                by_prefix[(key_namespace, identifier.split(":", 1)[0])].extend(entries)
            self._by_prefix = by_prefix
        return self._by_prefix.get((namespace, prefix), ())

    def trip(self, trip_id: str) -> TripInfo | None:
        return self._trips.get(trip_id)

    def active(self, service_id: str, day: date) -> bool:
        exception = self._exceptions.get((service_id, day))
        if exception is not None:
            return exception
        bit = 1 << (day.isoweekday() - 1)
        return any(
            valid_from <= day <= valid_to and mask & bit
            for valid_from, valid_to, mask in self._calendars.get(service_id, ())
        )

    def runs_on(self, trip_id: str, day: date) -> bool:
        trip = self._trips.get(trip_id)
        return trip is not None and self.active(trip.service_id, day)

    def span(self, trip_id: str) -> tuple[int, int] | None:
        return self._spans.get(trip_id)

    def run_span(self, run_key: str) -> tuple[int, int] | None:
        return self._run_spans.get(run_key)

    def calls(self, trip_ids: Collection[str]) -> dict[str, list[Call]]:
        """Ordered calls (stop place name, departure-first time) of `trip_ids`, cached."""

        missing = sorted(set(trip_ids) - self._calls.keys())
        if missing:
            calls = _read(
                self._path("trip_call"),
                ["trip_id", "sequence", "location_id", "scheduled_arrival", "scheduled_departure"],
                [("trip_id", "in", missing)],
            )
            locations = _read(self._path("location"), ["location_id", "parent_location_id", "name"])
            names: dict[str, str] = {}
            parent_of: dict[str, str | None] = {}
            for location_id, parent_id, name in _rows(locations, locations.column_names):
                names[location_id] = name
                parent_of[location_id] = parent_id
            found: dict[str, list[Call]] = {trip_id: [] for trip_id in missing}
            for trip_id, sequence, location_id, arrival, departure in _rows(
                calls, calls.column_names
            ):
                parent = parent_of.get(location_id)
                name = names.get(parent) if parent is not None else None
                found[trip_id].append(
                    Call(
                        sequence,
                        departure if departure is not None else arrival,
                        name or names.get(location_id) or "",
                    )
                )
            for trip_id, trip_calls in found.items():
                self._calls[trip_id] = sorted(trip_calls, key=lambda call: call.sequence)
        return {trip_id: self._calls[trip_id] for trip_id in trip_ids}
