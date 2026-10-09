"""The release index the core reads: static data per trip key, loaded lazily (pure data).

The worker and replay fill an `Index` with `obehy.realtime.index_sql.IndexLoader` before each
step for keys not seen yet; tests fill it with `tests.realtime.builder`. The core only reads it
through `IndexView` and treats a key it was not given as a programming error.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from obehy.realtime.model import Feed

KeyRef = tuple[str, str]  # (namespace, identifier)


class IndexMiss(LookupError):
    """The core asked for static data that was never loaded."""


@dataclass(frozen=True, slots=True)
class KeyEntry:
    """A source key applies to `trip_id` on the trip's service dates inside its validity."""

    trip_id: str
    valid_from: date
    valid_to: date

    def valid_on(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass(frozen=True, slots=True)
class Call:
    sequence: int
    location_id: str
    visit_n: int
    passenger_service: bool
    arrival: int | None
    departure: int | None
    distance_m: float | None

    @property
    def time(self) -> int:
        """The call's representative schedule time (departure, else arrival)."""

        value = self.departure if self.departure is not None else self.arrival
        if value is None:
            raise IndexMiss(f"call {self.sequence} at {self.location_id} has no time")
        return value


@dataclass(frozen=True, slots=True)
class Trip:
    trip_id: str
    route_id: str
    route_name: str
    mode: str
    service_id: str
    headsign: str | None
    shape_id: str | None
    calls: tuple[Call, ...]

    @property
    def start(self) -> int:
        first = self.calls[0]
        return first.departure if first.departure is not None else first.time

    @property
    def end(self) -> int:
        last = self.calls[-1]
        return last.arrival if last.arrival is not None else last.time


@dataclass(frozen=True, slots=True)
class Location:
    location_id: str
    name: str
    lon: float | None
    lat: float | None


@dataclass(frozen=True, slots=True)
class Shape:
    shape_id: str
    points: tuple[tuple[float, float], ...]  # (lon, lat)
    distances_m: tuple[float, ...]


class IndexView(Protocol):
    @property
    def release_id(self) -> str: ...

    @property
    def feed(self) -> Feed: ...

    def keys(self, namespace: str, identifier: str) -> tuple[KeyEntry, ...]: ...

    def namespace_has_prefix(self, namespace: str, prefix: str) -> bool: ...

    def trip(self, trip_id: str) -> Trip: ...

    def runs_on(self, service_id: str, day: date) -> bool: ...

    def location(self, location_id: str) -> Location: ...

    def shape(self, shape_id: str) -> Shape: ...


@dataclass(slots=True)
class Index:
    """A mutable, append-only index for one feed of one release."""

    release_id: str
    feed: Feed
    key_entries: dict[KeyRef, tuple[KeyEntry, ...]] = field(
        default_factory=dict[KeyRef, tuple[KeyEntry, ...]]
    )
    prefixes: dict[tuple[str, str], bool] = field(default_factory=dict[tuple[str, str], bool])
    trips: dict[str, Trip] = field(default_factory=dict[str, Trip])
    service_dates: dict[str, frozenset[date]] = field(default_factory=dict[str, frozenset[date]])
    loaded_dates: frozenset[date] = frozenset()
    locations: dict[str, Location] = field(default_factory=dict[str, Location])
    shapes: dict[str, Shape] = field(default_factory=dict[str, Shape])

    def missing(self, refs: Iterable[KeyRef]) -> set[KeyRef]:
        """Keys whose static data has not been loaded (or looked up and found absent) yet."""

        return {ref for ref in refs if ref not in self.key_entries}

    def keys(self, namespace: str, identifier: str) -> tuple[KeyEntry, ...]:
        try:
            return self.key_entries[(namespace, identifier)]
        except KeyError:
            raise IndexMiss(f"key {namespace}:{identifier} was not loaded") from None

    def namespace_has_prefix(self, namespace: str, prefix: str) -> bool:
        """Whether any key of `namespace` starts with `prefix:` (e.g. a CIS line exists)."""

        try:
            return self.prefixes[(namespace, prefix)]
        except KeyError:
            raise IndexMiss(f"prefix {namespace}:{prefix} was not loaded") from None

    def trip(self, trip_id: str) -> Trip:
        try:
            return self.trips[trip_id]
        except KeyError:
            raise IndexMiss(f"trip {trip_id} was not loaded") from None

    def runs_on(self, service_id: str, day: date) -> bool:
        if day not in self.loaded_dates:
            raise IndexMiss(f"service dates for {day} were not loaded")
        return day in self.service_dates.get(service_id, frozenset())

    def location(self, location_id: str) -> Location:
        try:
            return self.locations[location_id]
        except KeyError:
            raise IndexMiss(f"location {location_id} was not loaded") from None

    def shape(self, shape_id: str) -> Shape:
        try:
            return self.shapes[shape_id]
        except KeyError:
            raise IndexMiss(f"shape {shape_id} was not loaded") from None
