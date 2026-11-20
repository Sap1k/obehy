"""The index loader's rows from a release directory's serving files, without a database.

`obehy rt replay --from-files` on a development machine: the release's
`<feed>/serving/*.parquet` are the tables `obehy release load` copies into `static.*`, so the
same `IndexLoader` reads them through `FileRows` in place of `index_sql.SqlRows`. Each method
answers what its SQL counterpart selects, including what `release.load` derives on loading
(service dates from the calendar and its exceptions). The live worker always reads PostgreSQL.

Tables are read whole on first use (the national JDF feed's fit in about 2 GB) and filtered
in Arrow; only the rows asked for become Python objects.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

Row = tuple[Any, ...]

TRAIN_NUMBER = "czptt:train_number"


# Arrow's compute functions are typed too narrowly for scalars and value sets.
_compute = cast(Any, pc)


def _in(table: pa.Table, column: str, values: list[Any]) -> pa.Table:
    """The rows of `table` whose `column` is one of `values`."""

    data: Any = table.column(column)
    return table.filter(_compute.is_in(data, value_set=cast(Any, pa).array(values, data.type)))


def _equal(table: pa.Table, column: str, value: str) -> pa.Table:
    """The rows of `table` whose `column` is `value`."""

    return table.filter(_compute.equal(table.column(column), value))


def _rows(table: pa.Table, *columns: str) -> list[Row]:
    lists = [cast(list[Any], table.column(name).to_pylist()) for name in columns]
    return list(zip(*lists, strict=True))


def _line(identifier: str) -> str:
    """The leading part of a composite key, as SQL's `split_part(identifier, ':', 1)`."""

    return identifier.split(":", 1)[0]


class FileRows:
    """`index_sql.Rows` of one feed from its serving directory."""

    def __init__(self, serving: Path) -> None:
        if not (serving / "trip.parquet").is_file():
            raise FileNotFoundError(f"{serving} holds no serving tables")
        self.serving = serving

    def _read(self, table: str, *columns: str) -> pa.Table:
        return pq.read_table(self.serving / f"{table}.parquet", columns=list(columns))  # pyright: ignore[reportUnknownMemberType]

    @cached_property
    def _keys(self) -> pa.Table:
        return self._read(
            "source_key",
            "entity_kind",
            "namespace",
            "identifier",
            "public_id",
            "valid_from",
            "valid_to",
        )

    @cached_property
    def _trips(self) -> pa.Table:
        return self._read(
            "trip",
            "trip_id",
            "route_id",
            "service_id",
            "headsign",
            "shape_id",
            "run_key",
            "run_part",
        )

    @cached_property
    def _routes(self) -> dict[str, tuple[str | None, str, str | None]]:
        table = self._read("route", "route_id", "short_name", "mode", "agency_id")
        return {
            route_id: (short_name, mode, agency_id)
            for route_id, short_name, mode, agency_id in _rows(
                table, "route_id", "short_name", "mode", "agency_id"
            )
        }

    @cached_property
    def _calls(self) -> pa.Table:
        return self._read(
            "trip_call",
            "trip_id",
            "sequence",
            "location_id",
            "passenger_service",
            "boarding_point_id",
            "scheduled_arrival",
            "scheduled_departure",
            "pickup_type",
            "dropoff_type",
        )

    @cached_property
    def _locations(self) -> dict[str, tuple[str, float | None, float | None, str | None]]:
        table = self._read(
            "location", "location_id", "name", "longitude", "latitude", "coordinate_precision"
        )
        return {
            location_id: (name, lon, lat, precision)
            for location_id, name, lon, lat, precision in _rows(
                table, "location_id", "name", "longitude", "latitude", "coordinate_precision"
            )
        }

    @cached_property
    def _shape_points(self) -> pa.Table:
        return self._read("shape_point", "shape_id", "sequence", "longitude", "latitude")

    @cached_property
    def _calendar(self) -> dict[str, list[tuple[date, date, int]]]:
        table = self._read(
            "service_calendar", "service_id", "valid_from", "valid_to", "weekday_mask"
        )
        spans: dict[str, list[tuple[date, date, int]]] = defaultdict(list)
        for service_id, valid_from, valid_to, mask in _rows(
            table, "service_id", "valid_from", "valid_to", "weekday_mask"
        ):
            spans[service_id].append((valid_from, valid_to, mask))
        return spans

    @cached_property
    def _exceptions(self) -> pa.Table:
        return self._read("service_exception", "service_id", "service_date", "added")

    def _keys_in(self, namespace: str) -> pa.Table:
        return _equal(self._keys, "namespace", namespace)

    def keys(self, namespaces: list[str], identifiers: list[str]) -> list[Row]:
        wanted = set(zip(namespaces, identifiers, strict=True))
        found: list[Row] = []
        for namespace in sorted(set(namespaces)):
            table = _in(
                self._keys_in(namespace),
                "identifier",
                sorted({i for n, i in wanted if n == namespace}),
            )
            found.extend(
                _rows(
                    table,
                    "namespace",
                    "identifier",
                    "public_id",
                    "entity_kind",
                    "valid_from",
                    "valid_to",
                )
            )
        services = self._services
        found = [(*row, services.get(row[2]) if row[3] == "trip" else None) for row in found]
        return sorted(found, key=lambda r: (r[0], r[1], r[2], r[4]))

    @cached_property
    def _services(self) -> dict[str, str]:
        return dict(_rows(self._trips, "trip_id", "service_id"))

    def line_keys(self, namespace: str, lines: list[str]) -> list[Row]:
        wanted = set(lines)
        identifiers = {i for (i,) in _rows(self._keys_in(namespace), "identifier")}
        return sorted((_line(i), i) for i in identifiers if _line(i) in wanted)

    def trips(self, trip_ids: list[str]) -> list[Row]:
        numbers: dict[str, str] = {}
        for public_id, identifier in _rows(
            _in(self._keys_in(TRAIN_NUMBER), "public_id", trip_ids), "public_id", "identifier"
        ):
            numbers[public_id] = min(identifier, numbers.get(public_id, identifier))
        found: list[Row] = []
        for trip_id, route_id, service_id, headsign, shape_id, run_key, run_part in _rows(
            _in(self._trips, "trip_id", trip_ids),
            "trip_id",
            "route_id",
            "service_id",
            "headsign",
            "shape_id",
            "run_key",
            "run_part",
        ):
            route = self._routes.get(route_id)
            if route is None:
                continue  # the SQL joins the route
            short_name, mode, agency_id = route
            found.append(
                (
                    trip_id,
                    route_id,
                    short_name if short_name is not None else route_id,
                    mode,
                    service_id,
                    headsign,
                    shape_id,
                    run_key,
                    run_part,
                    agency_id,
                    numbers.get(trip_id),
                )
            )
        return found

    def calls(self, trip_ids: list[str]) -> list[Row]:
        rows = sorted(
            _rows(
                _in(self._calls, "trip_id", trip_ids),
                "trip_id",
                "sequence",
                "location_id",
                "passenger_service",
                "boarding_point_id",
                "scheduled_arrival",
                "scheduled_departure",
                "pickup_type",
                "dropoff_type",
            ),
            key=lambda r: (r[0], r[1]),
        )
        visits: dict[tuple[str, str], int] = defaultdict(int)
        found: list[Row] = []
        for (
            trip_id,
            sequence,
            location_id,
            passenger,
            post,
            arrival,
            departure,
            pickup,
            drop,
        ) in rows:
            visits[(trip_id, location_id)] += 1
            placed = self._locations.get(post) if post is not None else None
            found.append(
                (
                    trip_id,
                    sequence,
                    location_id,
                    visits[(trip_id, location_id)],
                    passenger,
                    arrival,
                    departure,
                    _or(_is(pickup, 3), _is(drop, 3)),
                    post if placed is not None and placed[3] == "exact" else None,
                )
            )
        return found

    def run_parts(self, run_keys: list[str]) -> list[Row]:
        rows = _rows(
            _in(self._trips, "run_key", run_keys), "run_key", "run_part", "trip_id", "service_id"
        )
        # NULLs sort last in PostgreSQL's ascending order.
        ordered = sorted(rows, key=lambda r: (r[0], r[1] is None, r[1] or 0, r[2]))
        return [(run_key, trip_id, service_id) for run_key, _, trip_id, service_id in ordered]

    def _location_keys(self, namespace: str) -> pa.Table:
        return _equal(self._keys_in(namespace), "entity_kind", "location")

    def station_keys(self, location_ids: list[str]) -> list[Row]:
        table = _in(self._location_keys("sr70"), "public_id", location_ids)
        return sorted(_rows(table, "identifier", "public_id"))

    def track_keys(self, codes: list[str]) -> list[Row]:
        wanted = set(codes)
        rows = _rows(self._location_keys("sr70:track"), "identifier", "public_id")
        return sorted(row for row in rows if _line(row[0]) in wanted)

    def locations(self, location_ids: list[str]) -> list[Row]:
        found: list[Row] = []
        for location_id in location_ids:
            row = self._locations.get(location_id)
            if row is not None:
                found.append((location_id, row[0], row[1], row[2]))
        return found

    def shape_points(self, shape_ids: list[str]) -> list[Row]:
        rows = _rows(
            _in(self._shape_points, "shape_id", shape_ids),
            "shape_id",
            "sequence",
            "longitude",
            "latitude",
        )
        return [(shape_id, lon, lat) for shape_id, _, lon, lat in sorted(rows, key=lambda r: r[:2])]

    def service_dates(self, service_ids: list[str], days: list[date]) -> list[Row]:
        """As `release.load` derives `static.service_date`: the calendar's weekdays inside its
        validity (bit 0 of the mask is Monday) less the removed dates, plus the added ones."""

        wanted = set(days)
        added: set[tuple[str, date]] = set()
        removed: set[tuple[str, date]] = set()
        for service_id, day, is_added in _rows(
            _in(self._exceptions, "service_id", service_ids), "service_id", "service_date", "added"
        ):
            if day in wanted:
                (added if is_added else removed).add((service_id, day))
        running = set(added)
        for service_id in service_ids:
            for valid_from, valid_to, mask in self._calendar.get(service_id, ()):
                for day in days:
                    if (
                        valid_from <= day <= valid_to
                        and mask & (1 << (day.isoweekday() - 1))
                        and (service_id, day) not in removed
                    ):
                        running.add((service_id, day))
        return sorted(running)


def _is(value: int | None, wanted: int) -> bool | None:
    return None if value is None else value == wanted


def _or(a: bool | None, b: bool | None) -> bool | None:
    """SQL's three-valued OR."""

    if a or b:
        return True
    return None if a is None or b is None else False
