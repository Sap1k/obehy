"""Vehicles beyond their positions (BASE_PLAN.md section 27), kept by the writer: which vehicle each
source report is, the attributes sources state live, and each vehicle's profile.

A report on a rail run is the train its number names, `train:<number>`: the coupled DÚK units, the
SŽ map's entry and PID's train are one train, which no unit or source names (DUK-Q8). It changes
where the run's number does. Any other report is a vehicle by, in turn: a registry key (its source's
vehicle ID, else an identifier it states: plate, EVN, series number); a confirmed co-located
vehicle, kept as reports join and leave its cluster (emit/pairing.py); else its own
`<source>:<source_vehicle_id>`. Identity and precedence are decided here, once, when an input
changes (a live attribute, a pairing, a train number, the registry), and written:
`rt.vehicle_state_current.vehicle_id`, `rt.vehicle_profile`, `rt.vehicle_pairing`. Readers only read
them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb

from obehy.realtime.emit.pairing import Evidence, Fix, Pairings
from obehy.realtime.model import (
    Feed,
    FeedState,
    JourneyKey,
    ObservationResult,
    VehicleAttribute,
    VehicleKey,
)
from obehy.realtime.runs import TRAIN_PREFIX, current_train
from obehy.realtime.times import Instant, local_date
from obehy.reference.precedence import Profile, Statement, profile
from obehy.reference.registry import REGISTRY_CHANNEL as REGISTRY_CHANNEL
from obehy.reference.registry import BoundBy, Registry, load_registry
from obehy.reference.vocabulary import Vocabulary, load_vocabulary

Report = tuple[str, str]  # (source, source vehicle ID)


def _journey_json(journey: JourneyKey) -> dict[str, str]:
    return {
        "feed": journey.feed,
        "namespace": journey.namespace,
        "key": journey.key,
        "service_date": journey.service_date.isoformat(),
    }


@dataclass(slots=True)
class VehicleBook:
    registry: Registry
    vocabulary: Vocabulary
    # Per report, the live value of each attribute and when it was last written.
    live: dict[Report, dict[str, tuple[str, Instant]]] = field(
        default_factory=dict[Report, dict[str, tuple[str, Instant]]]
    )
    identity: dict[Report, tuple[str, BoundBy | None]] = field(
        default_factory=dict[Report, tuple[str, BoundBy | None]]
    )
    members: dict[str, set[Report]] = field(default_factory=dict[str, set[Report]])
    # The train number of each report bound to a rail run.
    trains: dict[Report, str] = field(default_factory=dict[Report, str])
    pairings: Pairings = field(init=False)
    dirty: set[str] = field(default_factory=set[str])
    rewrite: bool = True  # the first flush rewrites every profile
    clock: Instant | None = None

    def __post_init__(self) -> None:
        self.pairings = Pairings(self.vocabulary.pairing)

    @classmethod
    def load(cls, connection: psycopg.Connection) -> VehicleBook:
        """The registry and the stored live attributes; every profile to be written."""

        vocabulary = load_vocabulary()
        book = cls(load_registry(connection), vocabulary)
        for a, va, b, vb, first, last, fixes, confirmed, journey in connection.execute(
            "SELECT source_a, source_vehicle_a, source_b, source_vehicle_b, first_seen,"
            " last_seen, fixes, confirmed, journey FROM rt.vehicle_pairing ORDER BY 1, 2, 3, 4"
        ).fetchall():
            book.pairings.pairs[((a, va), (b, vb))] = Evidence(
                first,
                last,
                fixes,
                JourneyKey(
                    journey["feed"],
                    journey["namespace"],
                    journey["key"],
                    date.fromisoformat(journey["service_date"]),
                ),
                confirmed,
            )
        for source, vehicle, attribute, value, last_seen in connection.execute(
            "SELECT source, source_vehicle_id, attribute, value, last_seen"
            " FROM rt.vehicle_attribute_observed ORDER BY 1, 2, 3"
        ).fetchall():
            book.live.setdefault((source, vehicle), {})[attribute] = (value, last_seen)
            book.clock = last_seen if book.clock is None else max(book.clock, last_seen)
        book._everything_dirty()
        return book

    def reload(self, connection: psycopg.Connection) -> None:
        """A new registry (`obehy ref import`): identities and profiles are redone."""

        self.registry = load_registry(connection)
        self._everything_dirty()

    def _everything_dirty(self) -> None:
        reports = sorted({*self.live, *self.identity})
        self.identity.clear()
        self.members.clear()
        if self.clock is not None:
            for report in reports:
                self.identify(report, local_date(self.clock))
        self.dirty |= set(self.registry.entries) | set(self.members)

    def _stated(self, report: Report) -> dict[str, str]:
        identifiers = self.vocabulary.identifiers
        return {a: v for a, (v, _) in self.live.get(report, {}).items() if a in identifiers}

    def _resolve(self, report: Report, day: date) -> tuple[str, BoundBy | None]:
        train = self.trains.get(report)
        if train is not None:
            return TRAIN_PREFIX + train, "train"
        bound = self.registry.bound(report[0], report[1], self._stated(report), day)
        if bound is not None:
            return bound
        members = self.pairings.cluster(report)
        if len(members) == 1:
            return f"{report[0]}:{report[1]}", None
        for member in members:
            found = self.registry.bound(member[0], member[1], self._stated(member), day)
            if found is not None:
                return found[0], "co_located"
        # Sticky: the member a report of the cluster is already known by stays the vehicle as
        # reports join and leave, so a vehicle's ID does not change under the map.
        names = {f"{s}:{v}" for s, v in members}
        held = sorted(
            known[0]
            for member in members
            if (known := self.identity.get(member)) is not None and known[0] in names
        )
        if held:
            return held[0], "co_located"
        anchor = min(members, key=lambda r: (self.vocabulary.source_rank("position", r[0]), r))
        return f"{anchor[0]}:{anchor[1]}", "co_located"

    def track(self, states: Mapping[Feed, FeedState]) -> None:
        """This tick's reports on journeys: the train each one on a rail run is, and pairing
        evidence from the fixes of the others; the identities a train or a confirmed or dropped
        pairing changes."""

        trains: dict[Report, str] = {}
        fixes: list[Fix] = []
        for _, state in sorted(states.items()):
            for vehicle, current in sorted(state.vehicles.items()):
                if current.binding is None:
                    continue
                report = (vehicle.source, vehicle.source_vehicle_id)
                self.clock = max(self.clock or current.last_seen, current.last_seen)
                instance = state.instances.get(current.binding.journey)
                if instance is not None and instance.journeys:
                    train = current_train(instance)
                    if train is not None:
                        trains[report] = train
                    continue  # the train number, not co-location, makes a run's reports one
                if current.position is not None and current.status == "running":
                    at = current.position_at or current.last_seen
                    position = current.position
                    fixes.append(
                        Fix(report, current.binding.journey, position.lon, position.lat, at)
                    )
        if trains != self.trains:
            changed = {r for r in {*trains, *self.trains} if trains.get(r) != self.trains.get(r)}
            self.trains = trains
            if self.clock is not None:
                for report in sorted(changed):
                    self.identify(report, local_date(self.clock))
        if not fixes:
            return
        now = max(f.at for f in fixes)
        moved = self.pairings.observe(fixes, now)
        for report in sorted({m for r in moved for m in self.pairings.cluster(r)}):
            self.identify(report, local_date(now))

    def identify(self, report: Report, day: date) -> tuple[str, BoundBy | None]:
        """Which vehicle a report is on `day`: the train of the rail run it is on, else by a
        registry key, else a co-located partner, else its own source form."""

        found = self._resolve(report, day)
        before = self.identity.get(report)
        if before != found:
            if before is not None:
                self.members.get(before[0], set()).discard(report)
                self.dirty.add(before[0])
            self.identity[report] = found
            self.members.setdefault(found[0], set()).add(report)
            self.dirty.add(found[0])
        return found

    def observe(self, results: list[ObservationResult]) -> list[tuple[str, str, str, str, Instant]]:
        """The live attributes to write: a changed value, or one not written for `touch_s`."""

        touch = timedelta(seconds=self.vocabulary.touch_s)
        rows: list[tuple[str, str, str, str, Instant]] = []
        for result in results:
            obs = result.observation
            vehicle = obs.first(VehicleKey)
            if vehicle is None:
                continue
            report = (obs.source, vehicle.source_vehicle_id)
            self.clock = obs.received_at if self.clock is None else max(self.clock, obs.received_at)
            values = self.live.setdefault(report, {})
            changed = False
            for fact in obs.all(VehicleAttribute):
                written = values.get(fact.attribute)
                if written is not None and written[0] == fact.value:
                    if obs.received_at - written[1] < touch:
                        continue
                else:
                    changed = True
                values[fact.attribute] = (fact.value, obs.received_at)
                rows.append((*report, fact.attribute, fact.value, obs.received_at))
            if changed:
                vehicle_id, _ = self.identify(report, local_date(obs.received_at))
                self.dirty.add(vehicle_id)
        return rows

    def profiles(self) -> list[Profile]:
        """The profiles of the vehicles whose inputs changed, and forget that they did."""

        built: list[Profile] = []
        for vehicle_id in sorted(self.dirty):
            live = [
                Statement(attribute, value, "live", source, observed_at=at)
                for source, vehicle in sorted(self.members.get(vehicle_id, ()))
                for attribute, (value, at) in sorted(self.live.get((source, vehicle), {}).items())
            ]
            entry = self.registry.entries.get(vehicle_id)
            if entry is None and not live:
                continue
            built.append(
                profile(
                    vehicle_id,
                    entry,
                    self.registry.values.get(vehicle_id, ()),
                    live,
                    self.vocabulary,
                )
            )
        self.dirty.clear()
        return built

    def write(
        self, connection: psycopg.Connection, observed: list[tuple[str, str, str, str, Instant]]
    ) -> None:
        """The live attributes, then the changed profiles (all of them on the first write)."""

        if observed:
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO rt.vehicle_attribute_observed AS o (source, source_vehicle_id,"
                    " attribute, value, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (source, source_vehicle_id, attribute) DO UPDATE SET"
                    " value = excluded.value,"
                    " first_seen = CASE WHEN o.value = excluded.value THEN o.first_seen"
                    "  ELSE excluded.first_seen END,"
                    " last_seen = greatest(o.last_seen, excluded.last_seen)",
                    [(*row[:4], row[4], row[4]) for row in observed],
                )
        self._write_pairings(connection)
        built = self.profiles()
        if self.rewrite:
            connection.execute("DELETE FROM rt.vehicle_profile")
            self.rewrite = False
        if not built:
            return
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO rt.vehicle_profile (vehicle_id, registered, public_label,"
                " manufacturer, model, low_floor, air_conditioning, wheelchair_accessible,"
                " operator, licence_plate, fleet_number, datasets, sources, attributes,"
                " last_stated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (vehicle_id) DO UPDATE SET (registered, public_label,"
                " manufacturer, model, low_floor, air_conditioning, wheelchair_accessible,"
                " operator, licence_plate, fleet_number, datasets, sources, attributes,"
                " last_stated) = (excluded.registered, excluded.public_label,"
                " excluded.manufacturer, excluded.model, excluded.low_floor,"
                " excluded.air_conditioning, excluded.wheelchair_accessible, excluded.operator,"
                " excluded.licence_plate, excluded.fleet_number, excluded.datasets,"
                " excluded.sources, excluded.attributes, excluded.last_stated)",
                [
                    (
                        p.vehicle_id,
                        p.registered,
                        p.public_label,
                        p.texts["manufacturer"],
                        p.texts["model"],
                        p.flags["low_floor"],
                        p.flags["air_conditioning"],
                        p.flags["wheelchair_accessible"],
                        p.texts["operator"],
                        p.texts["licence_plate"],
                        p.texts["fleet_number"],
                        list(p.datasets),
                        list(p.sources),
                        Jsonb(list(p.attributes)),
                        p.last_stated,
                    )
                    for p in built
                ],
            )

    def _write_pairings(self, connection: psycopg.Connection) -> None:
        changes = self.pairings.changes()
        gone = [pair for pair, evidence in sorted(changes.items()) if evidence is None]
        kept = [(pair, e) for pair, e in sorted(changes.items()) if e is not None]
        with connection.cursor() as cursor:
            if gone:
                cursor.executemany(
                    "DELETE FROM rt.vehicle_pairing WHERE (source_a, source_vehicle_a, source_b,"
                    " source_vehicle_b) = (%s, %s, %s, %s)",
                    [(*a, *b) for a, b in gone],
                )
            if kept:
                cursor.executemany(
                    "INSERT INTO rt.vehicle_pairing VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (source_a, source_vehicle_a, source_b, source_vehicle_b)"
                    " DO UPDATE SET (first_seen, last_seen, fixes, confirmed, journey) ="
                    " (excluded.first_seen, excluded.last_seen, excluded.fixes,"
                    " excluded.confirmed, excluded.journey)",
                    [
                        (
                            *a,
                            *b,
                            e.first_seen,
                            e.last_seen,
                            e.fixes,
                            e.confirmed,
                            Jsonb(_journey_json(e.journey)),
                        )
                        for (a, b), e in kept
                    ],
                )
