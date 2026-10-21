"""Consistency checks of GTFS-RT snapshots against their static GTFS (`obehy rt check-gtfs-rt`).

The subset of the GTFS-RT validator's error rules that our output can break: unknown trip IDs or
stop sequences, a trip not running on its start date, times decreasing along a trip, departure
before arrival, duplicate entity IDs, the header version, positions outside the region, and
alerts informing a route, stop or trip the static GTFS does not have.
MobilityData's realtime validator remains the reference; this runs without it.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as rt

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
REGION = (48.0, 52.0, 11.0, 20.0)  # lat_min, lat_max, lon_min, lon_max


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    if name not in archive.namelist():
        return iter(())
    return csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))


def _trip(entity: rt.FeedEntity) -> rt.TripDescriptor:
    return entity.trip_update.trip if entity.HasField("trip_update") else entity.vehicle.trip


def _trip_ids(message: rt.FeedMessage) -> Iterator[str]:
    for entity in message.entity:
        if entity.HasField("alert"):
            yield from (e.trip.trip_id for e in entity.alert.informed_entity if e.trip.trip_id)
        else:
            yield _trip(entity).trip_id


class _Static:
    """The parts of the static GTFS the snapshots refer to."""

    def __init__(self, gtfs: Path, trip_ids: set[str]) -> None:
        with zipfile.ZipFile(gtfs) as archive:
            self.trips = {
                r["trip_id"]: r["service_id"]
                for r in _rows(archive, "trips.txt")
                if r["trip_id"] in trip_ids
            }
            self.routes = {r["route_id"] for r in _rows(archive, "routes.txt")}
            self.stops = {r["stop_id"] for r in _rows(archive, "stops.txt")}
            self.sequences: dict[str, set[int]] = defaultdict(set)
            for r in _rows(archive, "stop_times.txt"):
                if r["trip_id"] in self.trips:
                    self.sequences[r["trip_id"]].add(int(r["stop_sequence"]))
            services = set(self.trips.values())
            self.calendar = {
                r["service_id"]: r
                for r in _rows(archive, "calendar.txt")
                if r["service_id"] in services
            }
            self.exceptions: dict[str, dict[str, str]] = defaultdict(dict)
            for r in _rows(archive, "calendar_dates.txt"):
                if r["service_id"] in services:
                    self.exceptions[r["service_id"]][r["date"]] = r["exception_type"]

    def runs(self, trip_id: str, ymd: str) -> bool:
        service = self.trips[trip_id]
        if ymd in self.exceptions[service]:
            return self.exceptions[service][ymd] == "1"
        row = self.calendar.get(service)
        if row is None or not row["start_date"] <= ymd <= row["end_date"]:
            return False
        day = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        return row[WEEKDAYS[day.weekday()]] == "1"


def check(gtfs: Path, snapshots: list[Path]) -> tuple[Counter[str], Counter[str]]:
    """(problems, totals) over every snapshot."""

    messages: list[rt.FeedMessage] = []
    for path in snapshots:
        message = rt.FeedMessage()
        message.ParseFromString(path.read_bytes())
        messages.append(message)
    static = _Static(gtfs, {trip_id for m in messages for trip_id in _trip_ids(m)})
    problems: Counter[str] = Counter()
    totals: Counter[str] = Counter(snapshots=len(messages), trips=len(static.trips))
    for message in messages:
        _check_message(message, static, problems, totals)
    return problems, totals


def _check_message(
    message: rt.FeedMessage, static: _Static, problems: Counter[str], totals: Counter[str]
) -> None:
    ids = [e.id for e in message.entity]
    if len(ids) != len(set(ids)):
        problems["duplicate entity id"] += 1
    if message.header.gtfs_realtime_version != "2.0":
        problems["header version"] += 1
    for entity in message.entity:
        totals["entities"] += 1
        if entity.HasField("alert"):
            totals["alerts"] += 1
            _check_alert(entity.alert, static, problems)
            continue
        trip = _trip(entity)
        if trip.trip_id not in static.trips:
            problems["unknown trip_id"] += 1
            continue
        if not static.runs(trip.trip_id, trip.start_date):
            problems["trip not running on start_date"] += 1
        if entity.HasField("vehicle"):
            _check_position(entity.vehicle.position, problems)
        else:
            _check_trip_update(entity.trip_update, static.sequences[trip.trip_id], problems, totals)


def _check_alert(alert: rt.Alert, static: _Static, problems: Counter[str]) -> None:
    if not alert.informed_entity:
        problems["alert informs nothing"] += 1
    for informed in alert.informed_entity:
        if informed.route_id and informed.route_id not in static.routes:
            problems["unknown alert route_id"] += 1
        if informed.stop_id and informed.stop_id not in static.stops:
            problems["unknown alert stop_id"] += 1
        if informed.trip.trip_id and informed.trip.trip_id not in static.trips:
            problems["unknown alert trip_id"] += 1


def _check_position(position: rt.Position, problems: Counter[str]) -> None:
    lat_min, lat_max, lon_min, lon_max = REGION
    if not (lat_min <= position.latitude <= lat_max and lon_min <= position.longitude <= lon_max):
        problems["position outside region"] += 1


def _check_trip_update(
    update: rt.TripUpdate, sequences: set[int], problems: Counter[str], totals: Counter[str]
) -> None:
    last, previous = 0, -1
    for stop in update.stop_time_update:
        totals["stop time updates"] += 1
        if stop.stop_sequence not in sequences:
            problems["unknown stop_sequence"] += 1
        if stop.stop_sequence <= previous:
            problems["stop_sequence not increasing"] += 1
        previous = stop.stop_sequence
        arrival = stop.arrival.time if stop.HasField("arrival") else None
        departure = stop.departure.time if stop.HasField("departure") else None
        if arrival is not None and departure is not None and departure < arrival:
            problems["departure before arrival"] += 1
        for value in (arrival, departure):
            if value is None:
                continue
            if value < last:
                problems["time decreases along trip"] += 1
            last = max(last, value)
