"""A journey's timetable laid on its path: trigger points and when the vehicle is due where.

Built once per trip and service date (`PlanCache`) and read by the tracker and the estimates.
`BASE_PLAN.md` sections 20.4 (lateness against the timetable window of a place) and 20.7
(trigger points).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.model import EventKind
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.path import Path, build_path
from obehy.realtime.times import Instant, ServiceTime

Scheduled = tuple[tuple[Instant | None, Instant | None], ...]  # per call: (arrival, departure)


@dataclass(frozen=True, slots=True)
class StopZone:
    """Where along the path a call's arrival and departure trigger; None where it has none."""

    arrival_m: float | None
    departure_m: float | None


@dataclass(frozen=True, slots=True)
class Trigger:
    """One event point along the path."""

    along_m: float
    call: int
    kind: EventKind


@dataclass(frozen=True, slots=True)
class Window:
    """When the timetable has the vehicle at a place; None is unbounded."""

    earliest: Instant | None
    latest: Instant | None

    def lateness_s(self, at: Instant) -> float:
        """Seconds behind (positive) or ahead of the window; 0 inside it."""

        if self.earliest is not None and at < self.earliest:
            return (at - self.earliest).total_seconds()
        if self.latest is not None and at > self.latest:
            return (at - self.latest).total_seconds()
        return 0.0

    def clamp(self, at: Instant) -> Instant:
        """The timetable time of being here at `at`: `at` clamped into the window."""

        if self.earliest is not None and at < self.earliest:
            return self.earliest
        if self.latest is not None and at > self.latest:
            return self.latest
        return at


def stop_zones(path: Path, policy: Policy) -> tuple[StopZone, ...]:
    """Each call's trigger points.

    Arrival triggers `arrival_radius_m` before the stop, departure `departure_margin_m` after it,
    but never past the midpoint to the neighbouring call: for stops closer together than the
    two margins, a departure must not trigger after the next arrival, so event times always
    follow call order.
    """

    radius = policy.lifecycle.arrival_radius_m
    margin = policy.lifecycle.departure_margin_m
    distances = path.call_distances_m
    last = len(distances) - 1
    zones: list[StopZone] = []
    for i, distance in enumerate(distances):
        arrival = departure = None
        if i > 0:
            arrival = max(distance - radius, (distances[i - 1] + distance) / 2)
        if i < last:
            departure = min(distance + margin, (distance + distances[i + 1]) / 2)
        zones.append(StopZone(arrival, departure))
    return tuple(zones)


@dataclass(frozen=True, slots=True)
class _Span:
    """A call's stretch of path (between its triggers) and its timetable window there."""

    start_m: float
    end_m: float
    window: Window


class Timetable:
    """When the timetable has the vehicle at a distance along the path.

    At a call (between its arrival and departure triggers) it is the dwell, [arrival,
    departure]; the first call has no earliest time, so waiting at the origin is never early.
    Between calls it is interpolated by distance from one call's departure trigger to the next
    call's arrival trigger. None where the timetable has no time.
    """

    __slots__ = ("_spans", "_starts")

    def __init__(self, path: Path, scheduled: Scheduled, zones: tuple[StopZone, ...]) -> None:
        spans: list[_Span] = []
        starts: list[float] = []
        for i, ((arrival, departure), zone) in enumerate(zip(scheduled, zones, strict=True)):
            earliest = None if i == 0 else (arrival if arrival is not None else departure)
            latest = departure if departure is not None else arrival
            start = -math.inf if zone.arrival_m is None else zone.arrival_m
            end = math.inf if zone.departure_m is None else zone.departure_m
            spans.append(_Span(start, end, Window(earliest, latest)))
            starts.append(start if i > 0 else path.call_distances_m[i])
        self._spans = tuple(spans)
        self._starts = tuple(starts)

    def window(self, along_m: float) -> Window | None:
        spans = self._spans
        if not spans:
            return None
        i = max(0, bisect_right(self._starts, along_m) - 1)
        span = spans[i]
        if along_m <= span.end_m or i == len(spans) - 1:
            if along_m >= span.start_m:
                return span.window
            return Window(None, span.window.latest)
        leave, reach = span.window.latest, spans[i + 1].window.earliest
        if leave is None or reach is None:
            return None
        gap = spans[i + 1].start_m - span.end_m
        share = 0.0 if gap <= 0 else (along_m - span.end_m) / gap
        planned = Instant(leave + (reach - leave) * share)
        return Window(planned, planned)

    def lateness_s(self, at: Instant, along_m: float) -> float:
        """Seconds behind (positive) or ahead of the timetable at a place; 0 inside a dwell."""

        window = self.window(along_m)
        return 0.0 if window is None else window.lateness_s(at)

    def planned(self, at: Instant, along_m: float) -> Instant | None:
        """The timetable time of being at a place at `at`."""

        window = self.window(along_m)
        return None if window is None else window.clamp(at)


@dataclass(frozen=True, slots=True)
class Plan:
    """A journey's path with its timetable laid on it."""

    path: Path
    scheduled: Scheduled
    zones: tuple[StopZone, ...]
    timetable: Timetable
    triggers: tuple[Trigger, ...]  # in path order
    trigger_distances: tuple[float, ...]

    def triggers_between(self, after_m: float, upto_m: float) -> tuple[Trigger, ...]:
        """Triggers with `after_m < along_m <= upto_m`, in path order."""

        first = bisect_right(self.trigger_distances, after_m)
        last = bisect_right(self.trigger_distances, upto_m)
        return self.triggers[first:last]

    def call_index_at(self, along_m: float) -> int:
        """The last call the vehicle has left: its departure trigger (or, for the last call,
        its arrival trigger) is at or behind `along_m`; -1 before the first departure."""

        index = -1
        for i, zone in enumerate(self.zones):
            trigger = zone.departure_m if zone.departure_m is not None else zone.arrival_m
            if trigger is not None and trigger <= along_m:
                index = i
        return index

    def scheduled_time(self, call: int, kind: EventKind) -> Instant | None:
        """The call's scheduled time of the event, falling back to its other time."""

        arrival, departure = self.scheduled[call]
        if kind == "arrival":
            return arrival if arrival is not None else departure
        return departure if departure is not None else arrival


def scheduled_instants(trip: Trip, day: date) -> Scheduled:
    """Each call's (arrival, departure) instants on service date `day`."""

    return tuple(
        (
            None if c.arrival is None else ServiceTime(day, c.arrival).instant(),
            None if c.departure is None else ServiceTime(day, c.departure).instant(),
        )
        for c in trip.calls
    )


def build_plan(path: Path, scheduled: Scheduled, policy: Policy) -> Plan:
    zones = stop_zones(path, policy)
    kinds: tuple[EventKind, EventKind] = ("arrival", "departure")
    triggers = sorted(
        (
            Trigger(along, i, kind)
            for i, zone in enumerate(zones)
            for kind, along in zip(kinds, (zone.arrival_m, zone.departure_m), strict=True)
            if along is not None
        ),
        key=lambda t: (t.along_m, t.call, t.kind != "arrival"),
    )
    return Plan(
        path,
        scheduled,
        zones,
        Timetable(path, scheduled, zones),
        tuple(triggers),
        tuple(t.along_m for t in triggers),
    )


@dataclass(slots=True)
class PlanCache:
    """Paths by trip and plans by journey for one release; deterministic, so caching never
    changes output."""

    release_id: str = ""
    paths: dict[str, Path] = field(default_factory=dict[str, Path])
    plans: dict[tuple[str, date], Plan] = field(default_factory=dict[tuple[str, date], Plan])

    def plan(self, trip: Trip, day: date, index: IndexView, policy: Policy) -> Plan:
        if index.release_id != self.release_id:
            self.release_id = index.release_id
            self.paths.clear()
            self.plans.clear()
        key = (trip.trip_id, day)
        value = self.plans.get(key)
        if value is None:
            path = self.paths.get(trip.trip_id)
            if path is None:
                path = build_path(trip, index)
                self.paths[trip.trip_id] = path
            value = build_plan(path, scheduled_instants(trip, day), policy)
            if len(self.plans) > 50_000:
                self.plans.clear()
            self.plans[key] = value
        return value
