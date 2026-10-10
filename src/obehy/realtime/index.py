"""The release index the core reads: static data per trip key, loaded lazily (pure data).

The worker and replay fill an `Index` with `obehy.realtime.index_sql.IndexLoader` before each
step for keys not seen yet; tests fill it with `tests.realtime.builder`. The core only reads it
through `IndexView` and treats a key it was not given as a programming error.

Rail (docs/R2_SLICE.md section 2): a CZPTT path (PA, `trip.run_key`) is published as trip parts
ordered by `run_part`, which keep the path's sequence numbers, so consecutive parts share their
junction call's sequence. The core follows the whole path as one **run trip** per service date:
`Index.run` concatenates the parts running that day, merges each junction call and drops
non-passenger points without any time. It is synthetic (`run:<PA>:<parts>`) and never output;
`Trip.parts` say which calls belong to which published part.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from obehy.realtime.model import Feed

KeyRef = tuple[str, str]  # (namespace, identifier)

# A composite source key and the namespace of its leading part, so a missing trip can be told
# apart from a missing line. The split is the source's own key format, never a public ID.
PARENT_NAMESPACE = {"cis:line_trip": "cis:line"}


def parent_ref(ref: KeyRef) -> KeyRef | None:
    parent = PARENT_NAMESPACE.get(ref[0])
    if parent is None or ":" not in ref[1]:
        return None
    return parent, ref[1].split(":", 1)[0]


class IndexMiss(LookupError):
    """The core asked for static data that was never loaded."""


@dataclass(frozen=True, slots=True)
class KeyEntry:
    """A source key applies to `public_id` (a trip or a route) on the service dates inside its
    validity."""

    public_id: str
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

    @property
    def time(self) -> int:
        """The call's representative schedule time (departure, else arrival)."""

        value = self.departure if self.departure is not None else self.arrival
        if value is None:
            raise IndexMiss(f"call {self.sequence} at {self.location_id} has no time")
        return value


@dataclass(frozen=True, slots=True)
class TripPart:
    """A published trip part of a run trip: its calls are `calls[first:last + 1]` of the run."""

    trip_id: str
    train_number: str | None
    first: int
    last: int


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
    # Rail: the CZPTT path (PA) this trip is a part of, its place in it and its train number.
    run_key: str | None = None
    run_part: int | None = None
    train_number: str | None = None
    # Only on a run trip: its published parts, in order.
    parts: tuple[TripPart, ...] = ()

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
    """Vertices and metres travelled at each, computed from the geometry. Feed-provided
    `shape_dist_traveled` values are never used: their unit is not fixed (PID overlays use km)."""

    shape_id: str
    points: tuple[tuple[float, float], ...]  # (lon, lat)
    distances_m: tuple[float, ...]


class IndexView(Protocol):
    @property
    def release_id(self) -> str: ...

    @property
    def feed(self) -> Feed: ...

    def keys(self, namespace: str, identifier: str) -> tuple[KeyEntry, ...]: ...

    def trip(self, trip_id: str) -> Trip: ...

    def runs_on(self, service_id: str, day: date) -> bool: ...

    def location(self, location_id: str) -> Location: ...

    def shape(self, shape_id: str) -> Shape: ...

    def run(self, trip: Trip, day: date) -> Trip:
        """The run trip of a trip part on `day`; a trip outside a run is its own run."""
        ...

    def location_key(self, location_id: str, namespace: str) -> str | None:
        """The location's source key in `namespace` (rail: `sr70`), if it has one."""
        ...

    def keyed_locations(self, namespace: str, identifier: str) -> tuple[str, ...]:
        """Locations a source key names (rail: `sr70:track` boarding points)."""
        ...


@dataclass(slots=True)
class Index:
    """A mutable, append-only index for one feed of one release."""

    release_id: str
    feed: Feed
    key_entries: dict[KeyRef, tuple[KeyEntry, ...]] = field(
        default_factory=dict[KeyRef, tuple[KeyEntry, ...]]
    )
    trips: dict[str, Trip] = field(default_factory=dict[str, Trip])
    service_dates: dict[str, frozenset[date]] = field(default_factory=dict[str, frozenset[date]])
    loaded_dates: frozenset[date] = frozenset()
    locations: dict[str, Location] = field(default_factory=dict[str, Location])
    shapes: dict[str, Shape] = field(default_factory=dict[str, Shape])
    # Rail: the trip parts of each loaded run (PA), in `run_part` order.
    runs: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    # Location source keys: by location and namespace, and the locations each key names.
    location_keys: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    keyed: dict[KeyRef, tuple[str, ...]] = field(default_factory=dict[KeyRef, tuple[str, ...]])

    def missing(self, refs: Iterable[KeyRef]) -> set[KeyRef]:
        """Keys whose static data has not been loaded (or looked up and found absent) yet."""

        return {ref for ref in refs if ref not in self.key_entries}

    def keys(self, namespace: str, identifier: str) -> tuple[KeyEntry, ...]:
        try:
            return self.key_entries[(namespace, identifier)]
        except KeyError:
            raise IndexMiss(f"key {namespace}:{identifier} was not loaded") from None

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

    def location_key(self, location_id: str, namespace: str) -> str | None:
        return self.location_keys.get(location_id, {}).get(namespace)

    def keyed_locations(self, namespace: str, identifier: str) -> tuple[str, ...]:
        return self.keyed.get((namespace, identifier), ())

    def add_location_key(self, location_id: str, namespace: str, identifier: str) -> None:
        self.location_keys.setdefault(location_id, {})[namespace] = identifier
        named = self.keyed.get((namespace, identifier), ())
        if location_id not in named:
            self.keyed[(namespace, identifier)] = tuple(sorted((*named, location_id)))

    def run(self, trip: Trip, day: date) -> Trip:
        if trip.run_key is None or trip.parts:
            return trip
        try:
            part_ids = self.runs[trip.run_key]
        except KeyError:
            raise IndexMiss(f"run {trip.run_key} was not loaded") from None
        parts = [self.trip(t) for t in part_ids]
        running = [p for p in parts if self.runs_on(p.service_id, day)] or [trip]
        run_id = f"run:{trip.run_key}:{','.join(str(p.run_part) for p in running)}"
        cached = self.trips.get(run_id)
        if cached is None:
            cached = run_trip(run_id, trip.run_key, running)
            self.trips[run_id] = cached
        return cached


def _timed(call: Call) -> bool:
    return call.passenger_service or call.arrival is not None or call.departure is not None


def run_trip(run_id: str, run_key: str, parts: list[Trip]) -> Trip:
    """The parts concatenated into one trip: the junction call shared by consecutive parts is
    merged (arrival of the earlier part, departure of the later), and railway points without
    any time are left out (they cannot be placed)."""

    calls: list[Call] = []
    spans: list[TripPart] = []
    for part in parts:
        first: int | None = None
        for call in part.calls:
            if not _timed(call):
                continue
            if calls and calls[-1].sequence == call.sequence:
                previous = calls[-1]
                calls[-1] = Call(
                    call.sequence,
                    call.location_id,
                    0,
                    previous.passenger_service or call.passenger_service,
                    previous.arrival if previous.arrival is not None else call.arrival,
                    call.departure if call.departure is not None else previous.departure,
                )
                index = len(calls) - 1
            else:
                calls.append(call)
                index = len(calls) - 1
            if first is None:
                first = index
        if first is not None:
            spans.append(TripPart(part.trip_id, part.train_number, first, len(calls) - 1))
    visits: dict[str, int] = {}
    numbered: list[Call] = []
    for call in calls:
        visits[call.location_id] = visits.get(call.location_id, 0) + 1
        numbered.append(
            Call(
                call.sequence,
                call.location_id,
                visits[call.location_id],
                call.passenger_service,
                call.arrival,
                call.departure,
            )
        )
    head, tail = parts[0], parts[-1]
    return Trip(
        run_id,
        head.route_id,
        head.route_name,
        head.mode,
        head.service_id,
        tail.headsign,
        None,
        tuple(numbered),
        run_key=run_key,
        parts=tuple(spans),
    )
