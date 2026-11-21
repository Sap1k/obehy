"""The diagnostic records of the output stream (`obehy rt compare --diagnostics|--quality`): what
the replay diagnostics (`replay_report.py`, `quality.py`, `quality_checks.py`, `evaluate.py`)
read of the core's state, so they read the stream and never the core. The record shapes are the
contract in `compare.py`'s docstring; the Rust worker writes the same.

- `predictor_sample` (`--diagnostics`, the rail feed only, every `PREDICTOR_SAMPLE_EVERY_S` on
  round multiples): each running rail run's arrival events and every predictor's arrival at
  the source's predicted next stop (`timeline.fusion`);
- `journey_sample` (`--quality`, every emit tick, per feed): each journey's lifecycle, progress,
  tracker reading, distances from its lead fix, and its calls and plan, the calls and plan only
  where they changed since the journey's previous sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from obehy.realtime.core import Context
from obehy.realtime.geo import distance_m
from obehy.realtime.index import IndexView
from obehy.realtime.model import CallState, FeedState, Instance, JourneyKey
from obehy.realtime.policy import PREDICTORS
from obehy.realtime.timeline.fusion import anchor_prediction, current_lateness
from obehy.realtime.timeline.path import point_at
from obehy.realtime.timeline.plan import Plan
from obehy.realtime.times import Instant, ServiceTime

PREDICTOR_SAMPLE_EVERY_S = 300
Record = dict[str, Any]
CallValues = tuple[
    Instant | None,
    Instant | None,
    Instant | None,
    Instant | None,
    Instant | None,
    Instant | None,
    bool,
    str,
]
CALL_FIELDS = (
    "arrival",
    "departure",
    "estimated_arrival",
    "estimated_departure",
    "passed_arrival",
    "passed_departure",
    "passenger",
    "status",
)


def predictor_sample(state: FeedState, index: IndexView, at: Instant) -> Record:
    """The `predictor_sample` record of the rail feed at `at`."""

    journeys: list[Record] = []
    for journey, instance in state.instances.items():
        if not instance.parts or instance.lifecycle != "running":
            continue
        trip = index.trip(instance.trip_id)
        arrivals = [[i, call.arrival.mid] for i, call in enumerate(instance.calls) if call.arrival]
        own, _ = current_lateness(instance)
        predicted: list[list[Any]] = []
        for predictor in PREDICTORS:
            if predictor == "propagate":
                target = anchor_prediction(instance, trip, index, "sz")
                if target is None or own is None:
                    continue
                i, lateness = target[0], own
            else:
                found = anchor_prediction(instance, trip, index, predictor)
                if found is None:
                    continue
                i, lateness = found
            scheduled = trip.calls[i].arrival
            if scheduled is None:
                continue
            when = ServiceTime(journey.service_date, scheduled).instant() + lateness
            predicted.append([predictor, i, when])
        if arrivals or predicted:
            journeys.append({"journey": journey, "arrivals": arrivals, "predicted": predicted})
    return {"kind": "predictor_sample", "at": at, "feed": state.feed, "journeys": journeys}


def _call_values(call: CallState) -> CallValues:
    return (
        None if call.arrival is None else call.arrival.mid,
        None if call.departure is None else call.departure.mid,
        call.estimated_arrival,
        call.estimated_departure,
        call.passed_arrival,
        call.passed_departure,
        call.passenger,
        call.status,
    )


def _plan_values(plan: Plan) -> Record:
    path = plan.path
    return {
        "scheduled": [list(times) for times in plan.scheduled],
        "zones": [[zone.arrival_m, zone.departure_m] for zone in plan.zones],
        "calls_m": list(path.call_distances_m),
        "shaped": path.shaped,
        "points": len(path.points),
    }


@dataclass(slots=True)
class _Seen:
    """What a journey's previous sample carried."""

    calls: tuple[CallState, ...]
    values: list[CallValues]
    plan: Plan
    plan_values: Record


@dataclass(slots=True)
class JourneySampler:
    """Writes `journey_sample` records; remembers each journey's last calls and plan, and forgets
    a journey absent from a sample of its feed (as the reader of the records does)."""

    seen: dict[JourneyKey, _Seen] = field(default_factory=dict[JourneyKey, _Seen])

    def sample(self, state: FeedState, ctx: Context, at: Instant) -> Record:
        journeys = [
            self._journey(journey, instance, state, ctx)
            for journey, instance in state.instances.items()
        ]
        for journey in [j for j in self.seen if j.feed == state.feed]:
            if journey not in state.instances:
                del self.seen[journey]
        return {"kind": "journey_sample", "at": at, "feed": state.feed, "journeys": journeys}

    def _journey(
        self, journey: JourneyKey, instance: Instance, state: FeedState, ctx: Context
    ) -> Record:
        trip = ctx.index.trip(instance.trip_id)
        plan = ctx.plans.plan(trip, journey.service_date, ctx.index, ctx.policy)
        progress, track = instance.progress, instance.track
        reached = progress.call_index if progress is not None else -1
        record: Record = {
            "journey": journey,
            "trip_id": instance.trip_id,
            "mode": instance.mode,
            "lifecycle": instance.lifecycle,
            "stale": instance.freshness.stale,
            "lost": instance.freshness.lost,
            "rail": bool(instance.parts),
            "lead": instance.lead is not None,
            "progress": None
            if progress is None
            else {"call": progress.call_index, "along_m": progress.distance_m},
            "track": None,
            "fix": self._fix(
                instance, plan, state, reached, ctx.policy.lifecycle.departure_margin_m
            ),
            "n": len(instance.calls),
        }
        if track is not None:
            best = track.hypotheses[0] if track.hypotheses else None
            record["track"] = {
                "seen_at": track.seen_at,
                "at_stop": track.at_stop is not None and track.at_stop.away is None,
                "lateness_s": track.lateness_s,
                "hypotheses": best is not None,
                "off_path": best is not None and best.off_path_since is not None,
            }
        record["calls"], record["plan"] = self._changes(journey, instance, plan)
        return record

    def _fix(
        self, instance: Instance, plan: Plan, state: FeedState, reached: int, margin: float
    ) -> Record | None:
        """Distances from the lead vehicle's position along a path of two points or more."""

        track, lead, path = instance.track, instance.lead, plan.path
        if track is None or lead is None or track.seen_at is None or len(path.points) < 2:
            return None
        vehicle = state.vehicles.get(lead.vehicle)
        if vehicle is None or vehicle.position is None:
            return None
        here = (vehicle.position.lon, vehicle.position.lat)
        calls = path.call_distances_m
        best = track.hypotheses[0] if track.hypotheses else None
        return {
            "best_m": None if best is None else distance_m(here, point_at(path, best.along_m)),
            "first_m": distance_m(here, point_at(path, calls[0])),
            "last_m": distance_m(here, point_at(path, calls[-1])),
            "near": [
                i
                for i in range(max(0, reached - 1), min(len(calls), reached + 3))
                if distance_m(here, point_at(path, calls[i])) <= margin
            ],
        }

    def _changes(
        self, journey: JourneyKey, instance: Instance, plan: Plan
    ) -> tuple[list[list[Any]], Record | None]:
        """The calls that changed since the journey's last sample (all on its first, or when
        their number changed) and its plan when that changed."""

        calls = instance.calls
        before = self.seen.get(journey)
        changed: list[list[Any]] = []
        if before is not None and before.calls is calls:
            values = before.values
        elif before is None or len(before.values) != len(calls):
            values = [_call_values(call) for call in calls]
            changed = [[i, v] for i, v in enumerate(values)]
        else:
            values = list(before.values)
            for i, call in enumerate(calls):
                if before.calls[i] is call:
                    continue
                value = _call_values(call)
                if value != values[i]:
                    values[i] = value
                    changed.append([i, value])
        shown: Record | None = None
        if before is not None and before.plan is plan:
            plan_values = before.plan_values
        else:
            plan_values = _plan_values(plan)
            if before is None or plan_values != before.plan_values:
                shown = plan_values
        self.seen[journey] = _Seen(calls, values, plan, plan_values)
        return [[i, dict(zip(CALL_FIELDS, v, strict=True))] for i, v in changed], shown
