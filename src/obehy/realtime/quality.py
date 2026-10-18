"""Prediction quality during replay: accuracy, flicker and dwell at stops. Pure.

Sampled every emit tick (`obehy rt replay --quality`), after estimates are brought up to date:

- accuracy: each running journey's predicted arrival at its next `HORIZON` calls, scored
  against the arrival event once it happens (MAE and bias, by mode and horizon);
- flicker: how often a call's predicted delay moves by `SIGNIFICANT` and then back, counted
  with hysteresis (a move is measured from the value at the previous move);
- dwell: at calls the timetable gives no dwell (arrival = departure), the time between the
  arrival and departure events and how much of it the next leg recovers (the lateness at the
  departure minus the lateness on arrival at the next call). If the timetable minute absorbs
  the dwell, the recovery follows the dwell;
- off-shape episodes (road vehicles on a real shape): stretches where the lead fix lies more
  than `OFF_SHAPE_M` from where the best reading places it, or the reading is held off the
  path. The bump is the peak measured lateness during the episode minus the lateness at the
  first fix back on the shape: lateness the matcher made up and then dropped.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from obehy.realtime.core import Context
from obehy.realtime.geo import distance_m
from obehy.realtime.model import FeedState, Instance, Interval, JourneyKey
from obehy.realtime.timeline.path import point_at
from obehy.realtime.timeline.plan import Plan, Scheduled
from obehy.realtime.times import Instant

HORIZON = 3
SIGNIFICANT = 30.0  # seconds
DWELL_BUCKETS = (15, 30, 45, 60, 90)  # upper bounds in seconds; beyond the last is the rest
OFF_SHAPE_M = 60.0  # about 2 sigma of GPS error and shape trust combined


def _middle(interval: Interval) -> Instant:
    return Instant(interval.lo + (interval.hi - interval.lo) / 2)


@dataclass(slots=True)
class _Call:
    """Predictions for one call not yet reached."""

    mode: str
    samples: list[tuple[int, Instant, Instant, float]] = field(
        default_factory=list[tuple[int, Instant, Instant, float]]
    )  # (horizon, made at, predicted arrival, predicted delay s)
    anchor: float | None = None  # predicted delay at the last significant move
    direction: int = 0
    moves: int = 0
    reversals: int = 0


@dataclass(slots=True)
class _Flicker:
    calls: int = 0
    moves: int = 0
    reversals: int = 0


@dataclass(slots=True)
class _Episode:
    since: Instant
    peak: float
    last: float


def _quantiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    n = len(ordered)
    return {str(q): round(ordered[min(n - 1, n * q // 100)], 1) for q in (10, 25, 50, 75, 90)}


DELAY_BUCKETS = ((-60, "early >1 min"), (0, "early"), (60, "late <1 min"), (180, "late 1-3 min"))


def _delay_bucket(seconds: float) -> str:
    for high, name in DELAY_BUCKETS:
        if seconds < high:
            return name
    return "late >3 min"


def _bucket(seconds: float) -> str:
    low = 0
    for high in DWELL_BUCKETS:
        if seconds < high:
            return f"{low}-{high}"
        low = high
    return f"{low}+"


@dataclass(slots=True)
class QualityLog:
    pending: dict[JourneyKey, dict[int, _Call]] = field(
        default_factory=dict[JourneyKey, dict[int, _Call]]
    )
    # (mode, horizon) -> [count, sum of absolute errors, sum of errors] in seconds
    # (mode, predicted delay bucket) -> the same sums, every horizon together
    by_delay: dict[tuple[str, str], list[float]] = field(
        default_factory=lambda: defaultdict[tuple[str, str], list[float]](lambda: [0.0, 0.0, 0.0])
    )
    errors: dict[tuple[str, int], list[float]] = field(
        default_factory=lambda: defaultdict[tuple[str, int], list[float]](lambda: [0.0, 0.0, 0.0])
    )
    flicker: dict[str, _Flicker] = field(default_factory=lambda: defaultdict(_Flicker))
    dwells: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict[str, list[float]](list)
    )
    recovery: dict[tuple[str, str], list[float]] = field(
        default_factory=lambda: defaultdict[tuple[str, str], list[float]](list)
    )
    dwell_seen: set[tuple[JourneyKey, int]] = field(default_factory=set[tuple[JourneyKey, int]])
    episodes: dict[JourneyKey, _Episode] = field(default_factory=dict[JourneyKey, _Episode])
    seen: dict[JourneyKey, Instant] = field(default_factory=dict[JourneyKey, Instant])
    bumps: dict[str, list[tuple[float, float]]] = field(
        default_factory=lambda: defaultdict[str, list[tuple[float, float]]](list)
    )  # (bump s, duration s)

    def sample(self, state: FeedState, ctx: Context, at: Instant) -> None:
        for journey, instance in state.instances.items():
            # A journey that has just finished is scored once more: its last call's arrival.
            finished = instance.lifecycle == "finished" and journey in self.pending
            if not finished and (instance.lifecycle != "running" or instance.freshness.stale):
                continue
            trip = ctx.index.trip(instance.trip_id)
            plan = ctx.plans.plan(trip, journey.service_date, ctx.index, ctx.policy)
            self._score(journey, instance)
            self._dwell(journey, instance, plan.scheduled)
            if finished:
                self.pending.pop(journey, None)
                continue
            self._predict(journey, instance, plan.scheduled, at)
            if not instance.parts and plan.path.shaped:
                self._shape(journey, instance, plan, state)
        for journey in [j for j in self.pending if j.feed == state.feed]:
            if journey not in state.instances:
                del self.pending[journey]  # forgotten before its calls were reached

    def _shape(self, journey: JourneyKey, instance: Instance, plan: Plan, state: FeedState) -> None:
        track, lead = instance.track, instance.lead
        if track is None or not track.hypotheses or lead is None or track.seen_at is None:
            return
        if self.seen.get(journey) == track.seen_at:
            return  # no new fix since the last sample
        self.seen[journey] = track.seen_at
        vehicle = state.vehicles.get(lead.vehicle)
        lateness = track.lateness_s
        if vehicle is None or vehicle.position is None or lateness is None:
            return
        best = track.hypotheses[0]
        here = (vehicle.position.lon, vehicle.position.lat)
        poor = best.off_path_since is not None or (
            distance_m(here, point_at(plan.path, best.along_m)) > OFF_SHAPE_M
        )
        episode = self.episodes.get(journey)
        if poor:
            if episode is None:
                self.episodes[journey] = _Episode(track.seen_at, lateness, lateness)
            else:
                episode.peak, episode.last = max(episode.peak, lateness), lateness
        elif episode is not None:
            del self.episodes[journey]
            duration = (track.seen_at - episode.since).total_seconds()
            self.bumps[instance.mode].append((episode.peak - lateness, duration))

    def _score(self, journey: JourneyKey, instance: Instance) -> None:
        pending = self.pending.get(journey)
        if not pending:
            return
        reached = instance.progress.call_index if instance.progress is not None else -1
        for i in [i for i in pending if instance.calls[i].passed_arrival or i <= reached]:
            call = pending.pop(i)
            if instance.calls[i].arrival is None:
                continue  # passed without an arrival event (a reception gap): nothing to score
            interval = instance.calls[i].arrival
            assert interval is not None
            actual = _middle(interval)
            for horizon, made, predicted, delay in call.samples:
                if made < actual:
                    error = (predicted - actual).total_seconds()
                    for total in (
                        self.errors[(call.mode, horizon)],
                        self.by_delay[(call.mode, _delay_bucket(delay))],
                    ):
                        total[0] += 1
                        total[1] += abs(error)
                        total[2] += error
            flicker = self.flicker[call.mode]
            flicker.calls += 1
            flicker.moves += call.moves
            flicker.reversals += call.reversals

    def _predict(
        self, journey: JourneyKey, instance: Instance, scheduled: Scheduled, at: Instant
    ) -> None:
        reached = instance.progress.call_index if instance.progress is not None else -1
        horizon = 0
        for i in range(reached + 1, len(instance.calls)):
            call = instance.calls[i]
            if not call.passenger or call.arrival is not None:
                continue
            planned = scheduled[i][0] or scheduled[i][1]
            if call.status != "predicted" or call.estimated_arrival is None or planned is None:
                break
            horizon += 1
            entry = self.pending.setdefault(journey, {}).get(i)
            if entry is None:
                entry = self.pending[journey][i] = _Call(instance.mode)
            delay = (call.estimated_arrival - planned).total_seconds()
            entry.samples.append((horizon, at, call.estimated_arrival, delay))
            if entry.anchor is None:
                entry.anchor = delay
            elif abs(delay - entry.anchor) >= SIGNIFICANT:
                direction = 1 if delay > entry.anchor else -1
                entry.moves += 1
                if entry.direction and direction != entry.direction:
                    entry.reversals += 1
                entry.direction, entry.anchor = direction, delay
            if horizon >= HORIZON:
                break

    def _dwell(self, journey: JourneyKey, instance: Instance, scheduled: Scheduled) -> None:
        reached = instance.progress.call_index if instance.progress is not None else -1
        calls = instance.calls
        for i in range(max(1, reached - 2), min(reached + 1, len(calls) - 1)):
            call = calls[i]
            arrival, departure = scheduled[i]
            if (journey, i) in self.dwell_seen or not call.passenger:
                continue
            if arrival is None or departure is None or arrival != departure:
                continue
            if call.arrival is None or call.departure is None:
                continue
            j = next((k for k in range(i + 1, len(calls)) if calls[k].passenger), None)
            if j is None or calls[j].arrival is None:
                continue
            following = scheduled[j][0] or scheduled[j][1]
            next_arrival = calls[j].arrival
            assert next_arrival is not None and following is not None
            self.dwell_seen.add((journey, i))
            arrived, left = _middle(call.arrival), _middle(call.departure)
            dwell = (left - arrived).total_seconds()
            leaving = (left - departure).total_seconds()
            reaching = (_middle(next_arrival) - following).total_seconds()
            self.dwells[instance.mode].append(dwell)
            self.recovery[(instance.mode, _bucket(dwell))].append(leaving - reaching)

    def report(self) -> dict[str, Any]:
        accuracy: dict[str, dict[str, Any]] = {}
        for (mode, horizon), (n, absolute, signed) in sorted(self.errors.items()):
            accuracy.setdefault(mode, {})[str(horizon)] = {
                "n": int(n),
                "mae_s": round(absolute / n, 1),
                "bias_s": round(signed / n, 1),
            }
        flicker = {
            mode: {
                "calls": f.calls,
                "moves_per_call": round(f.moves / f.calls, 3) if f.calls else None,
                "reversals_per_call": round(f.reversals / f.calls, 3) if f.calls else None,
            }
            for mode, f in sorted(self.flicker.items())
        }
        dwell: dict[str, dict[str, Any]] = {}
        for mode, values in sorted(self.dwells.items()):
            ordered = sorted(values)
            dwell[mode] = {
                "n": len(ordered),
                "p": _quantiles(ordered),
                "recovered_by_dwell": {
                    bucket: {"n": len(r), "median_s": round(median(r), 1)}
                    for (m, bucket), r in sorted(self.recovery.items())
                    if m == mode
                },
            }
        shape: dict[str, dict[str, Any]] = {}
        for mode, episodes in sorted(self.bumps.items()):
            bumps = [b for b, _ in episodes]
            shape[mode] = {
                "episodes": len(episodes),
                "duration_s": _quantiles([d for _, d in episodes]),
                "bump_s": _quantiles(bumps),
                "bumps_over_60_s": sum(b >= 60 for b in bumps),
            }
        by_delay: dict[str, dict[str, Any]] = {}
        for (mode, bucket), (n, absolute, signed) in sorted(self.by_delay.items()):
            by_delay.setdefault(mode, {})[bucket] = {
                "n": int(n),
                "mae_s": round(absolute / n, 1),
                "bias_s": round(signed / n, 1),
            }
        return {
            "accuracy": accuracy,
            "by_predicted_delay": by_delay,
            "flicker": flicker,
            "dwell": dwell,
            "off_shape": shape,
        }
