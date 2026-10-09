"""A tiny timetable builder for realtime tests (docs/R1_SLICE.md section 7).

    tt = timetable()
    tt.trip("582492:143", days=[date(2026, 10, 8)], calls=[("A", "23:50"), ("B", "24:20")])
    index = tt.index()

Stops not declared with `stop()` are placed 1 km apart eastwards in order of first use. Times
are wall-clock `HH:MM` (may exceed 24:00) or `(arrival, departure)` pairs. Every trip gets a
straight shape through its stops unless `shape=False`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from obehy.realtime.geo import cumulative_m
from obehy.realtime.index import Call, Index, KeyEntry, Location, Shape, Trip
from obehy.realtime.model import Feed

ORIGIN = (14.0, 50.0)
STEP_LON = 0.014  # about 1 km at 50° N

CallSpec = tuple[str, str] | tuple[str, str | None, str | None]


def hhmm(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


@dataclass(slots=True)
class TripSpec:
    trip_id: str
    namespace: str
    key: str
    days: frozenset[date]
    calls: tuple[Call, ...]
    route_id: str
    route_name: str
    mode: str
    headsign: str | None
    shape: bool
    valid_from: date
    valid_to: date


@dataclass(slots=True)
class Timetable:
    release_id: str = "test-release"
    feed: Feed = "jdf"
    namespace: str = "cis:line_trip"
    stops: dict[str, Location] = field(default_factory=dict[str, Location])
    trips: list[TripSpec] = field(default_factory=list[TripSpec])

    def stop(self, name: str, lon: float, lat: float) -> Timetable:
        self.stops[name] = Location(f"{self.feed}:{name}", name, lon, lat)
        return self

    def _location(self, name: str) -> Location:
        if name not in self.stops:
            n = len(self.stops)
            self.stops[name] = Location(
                f"{self.feed}:{name}", name, ORIGIN[0] + n * STEP_LON, ORIGIN[1]
            )
        return self.stops[name]

    def trip(
        self,
        key: str,
        *,
        days: Iterable[date],
        calls: Sequence[CallSpec],
        trip_id: str | None = None,
        namespace: str | None = None,
        mode: str = "bus",
        headsign: str | None = None,
        shape: bool = True,
        valid_from: date = date(2026, 1, 1),
        valid_to: date = date(2026, 12, 31),
    ) -> Timetable:
        visits: dict[str, int] = {}
        built: list[Call] = []
        for sequence, spec in enumerate(calls, start=1):
            name = spec[0]
            if len(spec) == 2:
                arrival = departure = hhmm(spec[1])
            else:
                arrival = hhmm(spec[1]) if spec[1] else None
                departure = hhmm(spec[2]) if spec[2] else None
            location = self._location(name)
            visits[name] = visits.get(name, 0) + 1
            built.append(
                Call(sequence, location.location_id, visits[name], True, arrival, departure, None)
            )
        line = key.split(":", 1)[0]
        self.trips.append(
            TripSpec(
                trip_id=trip_id or f"{self.feed}:t{len(self.trips) + 1}",
                namespace=namespace or self.namespace,
                key=key,
                days=frozenset(days),
                calls=tuple(built),
                route_id=f"{self.feed}:r{line}",
                route_name=line,
                mode=mode,
                headsign=headsign or calls[-1][0],
                shape=shape,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )
        return self

    def index(self) -> Index:
        """The fully loaded index: every key, trip and service date of the timetable."""

        index = Index(self.release_id, self.feed)
        by_location = {location.location_id: location for location in self.stops.values()}
        index.locations.update(by_location)
        all_days: set[date] = set()
        for spec in self.trips:
            calls = spec.calls
            shape_id = None
            if spec.shape:
                shape_id = f"{spec.trip_id}:shape"
                points = tuple(
                    (by_location[c.location_id].lon or 0.0, by_location[c.location_id].lat or 0.0)
                    for c in calls
                )
                distances = cumulative_m(points)
                index.shapes[shape_id] = Shape(shape_id, points, distances)
                calls = tuple(
                    Call(
                        c.sequence,
                        c.location_id,
                        c.visit_n,
                        c.passenger_service,
                        c.arrival,
                        c.departure,
                        distances[i],
                    )
                    for i, c in enumerate(calls)
                )
            service_id = f"{spec.trip_id}:service"
            index.trips[spec.trip_id] = Trip(
                spec.trip_id,
                spec.route_id,
                spec.route_name,
                spec.mode,
                service_id,
                spec.headsign,
                shape_id,
                calls,
            )
            index.service_dates[service_id] = spec.days
            all_days |= spec.days
            ref = (spec.namespace, spec.key)
            entry = KeyEntry(spec.trip_id, spec.valid_from, spec.valid_to)
            index.key_entries[ref] = (*index.key_entries.get(ref, ()), entry)
            prefix = spec.key.split(":", 1)[0]
            index.prefixes[(spec.namespace, prefix)] = True
        if all_days:
            index.loaded_dates = frozenset(
                days_between(min(all_days) - timedelta(days=3), max(all_days) + timedelta(days=3))
            )
        return index


def timetable(feed: Feed = "jdf", namespace: str = "cis:line_trip") -> Timetable:
    return Timetable(feed=feed, namespace=namespace)


def with_unknown(index: Index, *refs: tuple[str, str]) -> Index:
    """Mark keys as looked up and absent, and their line prefixes as unknown unless present."""

    for namespace, identifier in refs:
        index.key_entries.setdefault((namespace, identifier), ())
        prefix = identifier.split(":", 1)[0]
        index.prefixes.setdefault((namespace, prefix), False)
    return index
