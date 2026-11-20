"""Checks of stop events and shown times against the raw fixes, during replay. Pure.

Sampled with `quality.QualityLog` every emit tick. Where the vehicle stood is read from its
fixes alone (within the departure margin of a stop's point), not from the tracker's reading, so
the checks hold whatever places the events:

- `departed_while_there`: a departure timed before a later fix still at the stop;
- `stood_without_dwell`: fixes at the stop over a minute apart, departure within seconds of the
  arrival;
- `shown_departure_moved`: after the reading left a stop, its shown departure moved by half a
  minute or more;
- `same_departure`: two stops in a row, at least `APART_M` apart, shown leaving at the same time;
- `lost_near_end`: a journey ended without a time at its last stop, last seen near it;
- `started_without_time`: a journey first seen near its first stop ended with events after it
  but no time there.

Departure predictions are scored like the arrivals in `quality` (MAE and bias by horizon):
riders ask when a vehicle leaves. A few examples of each check are kept for trace fixtures.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from obehy.realtime.core import Context
from obehy.realtime.geo import distance_m
from obehy.realtime.model import FeedState, Instance, JourneyKey
from obehy.realtime.timeline.path import point_at
from obehy.realtime.timeline.plan import Plan
from obehy.realtime.times import Instant

HORIZON = 3
EXAMPLES = 25
TOLERANCE = timedelta(seconds=5)  # a departure this little before a fix at the stop is fine
STOOD = timedelta(seconds=60)
NO_DWELL = timedelta(seconds=10)
MOVED_S = 30.0
SAME = timedelta(seconds=5)
APART_M = 100.0
SETTLED = timedelta(seconds=180)  # a departure is judged this long after it, or two stops on
NEAR_END_M = 500.0
NEAR_START_M = 1000.0  # first seen this close to its first stop: it was seen starting

CallKey = tuple[JourneyKey, int]


@dataclass(slots=True)
class _Counts:
    departures: int = 0
    departed_while_there: int = 0
    stood_without_dwell: int = 0
    shown_departure_moved: int = 0
    pairs: int = 0
    same_departure: int = 0
    ended: int = 0
    ended_with_time: int = 0
    lost_near_end: int = 0
    started: int = 0  # ended, first seen near its first stop
    started_without_time: int = 0  # of those, with later events but no time at the first stop


@dataclass(slots=True)
class Checks:
    counts: dict[str, _Counts] = field(default_factory=lambda: defaultdict(_Counts))
    examples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict[str, list[dict[str, Any]]](list)
    )
    # Per call: the first and last fix within the departure margin of its stop.
    near: dict[CallKey, tuple[Instant, Instant]] = field(
        default_factory=dict[CallKey, tuple[Instant, Instant]]
    )
    # Per call the reading has left: the least and greatest departure shown since.
    shown: dict[CallKey, tuple[Instant, Instant]] = field(
        default_factory=dict[CallKey, tuple[Instant, Instant]]
    )
    judged: set[CallKey] = field(default_factory=set[CallKey])
    pairs: set[CallKey] = field(default_factory=set[CallKey])
    same: set[CallKey] = field(default_factory=set[CallKey])
    fix_at: dict[JourneyKey, Instant] = field(default_factory=dict[JourneyKey, Instant])
    # Running journeys: mode, trip, and how far the last fix was from the last stop.
    running: dict[JourneyKey, tuple[str, str, float | None]] = field(
        default_factory=dict[JourneyKey, tuple[str, str, float | None]]
    )
    # How far each journey's first fix was from its first stop.
    first_seen_m: dict[JourneyKey, float] = field(default_factory=dict[JourneyKey, float])
    # Departure predictions not yet scored: (horizon, made at, predicted departure).
    pending: dict[CallKey, list[tuple[int, Instant, Instant]]] = field(
        default_factory=dict[CallKey, list[tuple[int, Instant, Instant]]]
    )
    # (mode, horizon) -> [count, sum of absolute errors, sum of errors] in seconds
    errors: dict[tuple[str, int], list[float]] = field(
        default_factory=lambda: defaultdict[tuple[str, int], list[float]](lambda: [0.0, 0.0, 0.0])
    )

    def sample(self, state: FeedState, ctx: Context, at: Instant) -> None:
        margin = ctx.policy.lifecycle.departure_margin_m
        for journey, instance in state.instances.items():
            if instance.parts:
                continue  # rail runs: events come from sources, not from fixes at stops
            known = journey in self.running
            if instance.lifecycle != "running" or instance.freshness.lost:
                if known:
                    self._ended(journey, instance)
                continue
            trip = ctx.index.trip(instance.trip_id)
            plan = ctx.plans.plan(trip, journey.service_date, ctx.index, ctx.policy)
            if len(plan.path.points) < 2:
                continue
            reached = instance.progress.call_index if instance.progress is not None else -1
            self._fix(journey, instance, plan, state, reached, margin)
            self._shown(journey, instance, reached)
            self._judge(journey, instance, reached, at)
            if not instance.freshness.stale:
                self._ahead(journey, instance, plan, reached, at)
        for journey in [j for j in self.running if j.feed == state.feed]:
            if journey not in state.instances:
                self._ended(journey, None)

    def _example(self, check: str, journey: JourneyKey, trip_id: str, **details: Any) -> None:
        if len(self.examples[check]) < EXAMPLES:
            self.examples[check].append(
                {
                    "key": journey.key,
                    "service_date": str(journey.service_date),
                    "trip_id": trip_id,
                    **details,
                }
            )

    def _fix(
        self,
        journey: JourneyKey,
        instance: Instance,
        plan: Plan,
        state: FeedState,
        reached: int,
        margin: float,
    ) -> None:
        """Note which stops the newest fix of the lead vehicle is at."""

        track, lead = instance.track, instance.lead
        if track is None or lead is None or track.seen_at is None:
            return
        vehicle = state.vehicles.get(lead.vehicle)
        if vehicle is None or vehicle.position is None:
            return
        here = (vehicle.position.lon, vehicle.position.lat)
        calls = plan.path.call_distances_m
        end = distance_m(here, point_at(plan.path, calls[-1]))
        self.running[journey] = (instance.mode, instance.trip_id, end)
        if journey not in self.first_seen_m:
            self.first_seen_m[journey] = distance_m(here, point_at(plan.path, calls[0]))
        if self.fix_at.get(journey) == track.seen_at:
            return
        self.fix_at[journey] = track.seen_at
        for i in range(max(0, reached - 1), min(len(calls), reached + 3)):
            if distance_m(here, point_at(plan.path, calls[i])) <= margin:
                first, _ = self.near.get((journey, i), (track.seen_at, track.seen_at))
                self.near[(journey, i)] = (first, track.seen_at)

    def _shown(self, journey: JourneyKey, instance: Instance, reached: int) -> None:
        for i in range(max(0, reached - 2), reached + 1):
            call = instance.calls[i]
            if (journey, i) in self.judged or call.estimated_departure is None:
                continue
            low, high = self.shown.get(
                (journey, i), (call.estimated_departure, call.estimated_departure)
            )
            self.shown[(journey, i)] = (
                min(low, call.estimated_departure),
                max(high, call.estimated_departure),
            )

    def _judge(self, journey: JourneyKey, instance: Instance, reached: int, at: Instant) -> None:
        for i in range(max(0, reached - 3), min(reached + 1, len(instance.calls) - 1)):
            call = instance.calls[i]
            left = call.passed_departure
            if (journey, i) in self.judged or left is None or not call.passenger:
                continue
            if reached < i + 2 and at - left < SETTLED:
                continue  # fixes at the stop may still come
            self.judged.add((journey, i))
            counts = self.counts[instance.mode]
            counts.departures += 1
            near = self.near.pop((journey, i), None)
            if near is not None and near[1] > left + TOLERANCE:
                counts.departed_while_there += 1
                self._example(
                    "departed_while_there",
                    journey,
                    instance.trip_id,
                    call=i,
                    departure=left.isoformat(),
                    last_fix_there=near[1].isoformat(),
                )
            arrived = call.passed_arrival
            if (
                near is not None
                and arrived is not None
                and near[1] - near[0] >= STOOD
                and left - arrived < NO_DWELL
            ):
                counts.stood_without_dwell += 1
                self._example(
                    "stood_without_dwell",
                    journey,
                    instance.trip_id,
                    call=i,
                    departure=left.isoformat(),
                    first_fix_there=near[0].isoformat(),
                    last_fix_there=near[1].isoformat(),
                )
            shown = self.shown.pop((journey, i), None)
            if shown is not None and (shown[1] - shown[0]).total_seconds() >= MOVED_S:
                counts.shown_departure_moved += 1
                self._example(
                    "shown_departure_moved",
                    journey,
                    instance.trip_id,
                    call=i,
                    least=shown[0].isoformat(),
                    greatest=shown[1].isoformat(),
                )
            self._score(journey, i, instance)

    def _score(self, journey: JourneyKey, i: int, instance: Instance) -> None:
        samples = self.pending.pop((journey, i), None)
        departure = instance.calls[i].departure
        if not samples or departure is None:
            return  # left without a departure event (a reception gap): nothing to score
        actual = departure.mid
        for horizon, made, predicted in samples:
            if made < actual:
                error = (predicted - actual).total_seconds()
                total = self.errors[(instance.mode, horizon)]
                total[0] += 1
                total[1] += abs(error)
                total[2] += error

    def _ahead(
        self, journey: JourneyKey, instance: Instance, plan: Plan, reached: int, at: Instant
    ) -> None:
        """Sample the departures predicted ahead; note stops in a row leaving together."""

        calls, distances = instance.calls, plan.path.call_distances_m
        horizon = 0
        previous: int | None = None
        for i in range(max(0, reached), len(calls) - 1):
            call = calls[i]
            if not call.passenger or call.estimated_departure is None:
                continue
            unleft = call.passed_departure is None and call.departure is None
            if unleft:
                horizon += 1
                self.pending.setdefault((journey, i), []).append(
                    (horizon, at, call.estimated_departure)
                )
            if previous is not None and distances[i] - distances[previous] >= APART_M:
                before = calls[previous].estimated_departure
                if before is not None and unleft:
                    self.pairs.add((journey, previous))
                    if call.estimated_departure - before < SAME and (
                        (journey, previous) not in self.same
                    ):
                        self.same.add((journey, previous))
                        self._example(
                            "same_departure",
                            journey,
                            instance.trip_id,
                            call=previous,
                            at=at.isoformat(),
                            shown=before.isoformat(),
                            hop_m=round(distances[i] - distances[previous]),
                        )
            previous = i
            if horizon >= HORIZON:
                break

    def _ended(self, journey: JourneyKey, instance: Instance | None) -> None:
        mode, trip_id, end_m = self.running.pop(journey)
        self.fix_at.pop(journey, None)
        counts = self.counts[mode]
        counts.ended += 1
        near_start = self.first_seen_m.pop(journey, None)
        if instance is not None and near_start is not None and near_start <= NEAR_START_M:
            counts.started += 1
            first = instance.calls[0]
            later = any(
                c.passed_arrival is not None or c.passed_departure is not None
                for c in instance.calls[1:]
            )
            if later and first.departure is None and first.passed_departure is None:
                counts.started_without_time += 1
                self._example("started_without_time", journey, trip_id)
        last = instance.calls[-1] if instance is not None else None
        if last is not None and (last.arrival is not None or last.passed_arrival is not None):
            counts.ended_with_time += 1
        elif instance is not None and end_m is not None and end_m <= NEAR_END_M:
            counts.lost_near_end += 1
            self._example("lost_near_end", journey, trip_id, last_fix_from_end_m=round(end_m))

    def report(self) -> dict[str, Any]:
        by_mode: dict[str, dict[str, Any]] = {}
        for mode, counts in sorted(self.counts.items()):
            by_mode[mode] = {
                "departures": counts.departures,
                "departed_while_there": counts.departed_while_there,
                "stood_without_dwell": counts.stood_without_dwell,
                "shown_departure_moved": counts.shown_departure_moved,
                "ended": counts.ended,
                "ended_with_time": counts.ended_with_time,
                "lost_near_end": counts.lost_near_end,
                "started": counts.started,
                "started_without_time": counts.started_without_time,
            }
        accuracy: dict[str, dict[str, Any]] = {}
        for (mode, horizon), (n, absolute, signed) in sorted(self.errors.items()):
            accuracy.setdefault(mode, {})[str(horizon)] = {
                "n": int(n),
                "mae_s": round(absolute / n, 1),
                "bias_s": round(signed / n, 1),
            }
        return {
            "events": by_mode,
            "same_departure": {"pairs": len(self.pairs), "same": len(self.same)},
            "departure_accuracy": accuracy,
            "examples": {check: found for check, found in sorted(self.examples.items())},
        }
