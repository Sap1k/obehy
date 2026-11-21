"""Prediction quality during replay: accuracy, flicker and dwell at stops. Pure.

Read from the output stream's `journey_sample` records (`obehy-rt compare --quality`;
`journey_view.py`), one per feed every emit tick, after estimates are brought up to date; never
from the core:

- accuracy: each running journey's predicted arrival at its next `HORIZON` calls, scored
  against the arrival event once it happens (MAE and bias, by mode and horizon);
- flicker: how often a call's predicted delay moves by `SIGNIFICANT` and then back, counted
  with hysteresis (a move is measured from the value at the previous move);
- dwell: at calls the timetable gives no dwell (arrival = departure), the time between the
  arrival and departure events and how much of it the next leg recovers (the lateness at the
  departure minus the lateness on arrival at the next call). If the timetable minute absorbs
  the dwell, the recovery follows the dwell;
- event and shown-time checks against the raw fixes, and departure accuracy
  (`quality_checks`);
- off-shape episodes (road vehicles on a real shape): stretches where the lead fix lies more
  than `OFF_SHAPE_M` from where the best reading places it, or the reading is held off the
  path. The bump is the peak measured lateness during the episode minus the lateness at the
  first fix back on the shape: lateness the matcher made up and then dropped.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from obehy.realtime.journey_view import Journey, Key
from obehy.realtime.quality_checks import Checks
from obehy.realtime_config import Instant

HORIZON = 3
SIGNIFICANT = 30.0  # seconds
DWELL_BUCKETS = (15, 30, 45, 60, 90)  # upper bounds in seconds; beyond the last is the rest
OFF_SHAPE_M = 60.0  # about 2 sigma of GPS error and shape trust combined


@dataclass(slots=True)
class _Call:
    """Predictions for one call not yet reached."""

    mode: str
    samples: list[tuple[int, Instant, Instant, float, tuple[str, str]]] = field(
        default_factory=list[tuple[int, Instant, Instant, float, tuple[str, str]]]
    )  # (horizon, made at, predicted arrival, predicted delay s, (where, fix age))
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


def _context(journey: Journey, at: Instant) -> tuple[str, str]:
    """Where the vehicle is when predicting (at a stop, or how far along the leg) and how old
    its fix is: the next call's accuracy is split by both."""

    track, progress, zones = journey.track, journey.progress, journey.plan.zones
    seen = track.seen_at if track is not None else None
    age = "unknown" if seen is None else _age_bucket((at - seen).total_seconds())
    if track is not None and track.at_stop:
        return "at stop", age
    if progress is None:
        return "unknown", age
    i, distance = progress
    if i < 0 or i + 1 >= len(zones):
        return "unknown", age
    start, end = zones[i][1], zones[i + 1][0]
    if start is None or end is None or end <= start:
        return "unknown", age
    share = (distance - start) / (end - start)
    return f"leg {min(3, max(0, int(share * 4))) * 25}%", age


def _age_bucket(seconds: float) -> str:
    for high in (15, 30, 45, 60, 90):
        if seconds < high:
            return f"fix <{high} s"
    return "fix 90 s+"


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
    pending: dict[Key, dict[int, _Call]] = field(default_factory=dict[Key, dict[int, _Call]])
    # (mode, horizon) -> [count, sum of absolute errors, sum of errors] in seconds
    # (mode, predicted delay bucket) -> the same sums, every horizon together
    by_delay: dict[tuple[str, str], list[float]] = field(
        default_factory=lambda: defaultdict[tuple[str, str], list[float]](lambda: [0.0, 0.0, 0.0])
    )
    # (mode, where the vehicle was, how old its fix) -> the same sums, next call only
    by_where: dict[tuple[str, str, str], list[float]] = field(
        default_factory=lambda: defaultdict[tuple[str, str, str], list[float]](
            lambda: [0.0, 0.0, 0.0]
        )
    )
    # (mode, horizon) -> every signed error, for quantiles: a tail of minutes moves the mean
    signed: dict[tuple[str, int], list[float]] = field(
        default_factory=lambda: defaultdict[tuple[str, int], list[float]](list)
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
    dwell_seen: set[tuple[Key, int]] = field(default_factory=set[tuple[Key, int]])
    episodes: dict[Key, _Episode] = field(default_factory=dict[Key, _Episode])
    seen: dict[Key, Instant] = field(default_factory=dict[Key, Instant])
    bumps: dict[str, list[tuple[float, float]]] = field(
        default_factory=lambda: defaultdict[str, list[tuple[float, float]]](list)
    )  # (bump s, duration s)
    checks: Checks = field(default_factory=Checks)

    def sample(self, feed: str, journeys: Sequence[Journey], at: Instant) -> None:
        """One feed's `journey_sample` at `at` (`JourneyView.read`)."""

        self.checks.sample(feed, journeys, at)
        for sampled in journeys:
            journey = sampled.key
            # A journey that has just finished is scored once more: its last call's arrival.
            finished = sampled.lifecycle == "finished" and journey in self.pending
            if not finished and (sampled.lifecycle != "running" or sampled.stale):
                continue
            self._score(sampled)
            self._dwell(sampled)
            if finished:
                self.pending.pop(journey, None)
                continue
            self._predict(sampled, at)
            if not sampled.rail and sampled.plan.shaped:
                self._shape(sampled)
        present = {j.key for j in journeys}
        for journey in [j for j in self.pending if j.feed == feed]:
            if journey not in present:
                del self.pending[journey]  # forgotten before its calls were reached

    def _shape(self, sampled: Journey) -> None:
        journey, track, fix = sampled.key, sampled.track, sampled.fix
        if track is None or not track.hypotheses or not sampled.lead or track.seen_at is None:
            return
        if self.seen.get(journey) == track.seen_at:
            return  # no new fix since the last sample
        self.seen[journey] = track.seen_at
        lateness = track.lateness_s
        if fix is None or fix.best_m is None or lateness is None:
            return
        poor = track.off_path or fix.best_m > OFF_SHAPE_M
        episode = self.episodes.get(journey)
        if poor:
            if episode is None:
                self.episodes[journey] = _Episode(track.seen_at, lateness, lateness)
            else:
                episode.peak, episode.last = max(episode.peak, lateness), lateness
        elif episode is not None:
            del self.episodes[journey]
            duration = (track.seen_at - episode.since).total_seconds()
            self.bumps[sampled.mode].append((episode.peak - lateness, duration))

    def _score(self, sampled: Journey) -> None:
        pending = self.pending.get(sampled.key)
        if not pending:
            return
        reached, calls = sampled.reached, sampled.calls
        for i in [i for i in pending if calls[i].passed_arrival or i <= reached]:
            call = pending.pop(i)
            actual = calls[i].arrival
            if actual is None:
                continue  # passed without an arrival event (a reception gap): nothing to score
            for horizon, made, predicted, delay, (where, age) in call.samples:
                if made < actual:
                    error = (predicted - actual).total_seconds()
                    totals = [
                        self.errors[(call.mode, horizon)],
                        self.by_delay[(call.mode, _delay_bucket(delay))],
                    ]
                    self.signed[(call.mode, horizon)].append(error)
                    if horizon == 1:
                        totals += [
                            self.by_where[(call.mode, where, "all")],
                            self.by_where[(call.mode, "all", age)],
                        ]
                    for total in totals:
                        total[0] += 1
                        total[1] += abs(error)
                        total[2] += error
            flicker = self.flicker[call.mode]
            flicker.calls += 1
            flicker.moves += call.moves
            flicker.reversals += call.reversals

    def _predict(self, sampled: Journey, at: Instant) -> None:
        journey, scheduled, reached = sampled.key, sampled.plan.scheduled, sampled.reached
        context = _context(sampled, at)
        horizon = 0
        for i in range(reached + 1, len(sampled.calls)):
            call = sampled.calls[i]
            if not call.passenger or call.arrival is not None:
                continue
            planned = scheduled[i][0] or scheduled[i][1]
            if call.status != "predicted" or call.estimated_arrival is None or planned is None:
                break
            horizon += 1
            entry = self.pending.setdefault(journey, {}).get(i)
            if entry is None:
                entry = self.pending[journey][i] = _Call(sampled.mode)
            delay = (call.estimated_arrival - planned).total_seconds()
            entry.samples.append((horizon, at, call.estimated_arrival, delay, context))
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

    def _dwell(self, sampled: Journey) -> None:
        journey, reached, calls = sampled.key, sampled.reached, sampled.calls
        scheduled = sampled.plan.scheduled
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
            arrived, left = call.arrival, call.departure
            dwell = (left - arrived).total_seconds()
            leaving = (left - departure).total_seconds()
            reaching = (next_arrival - following).total_seconds()
            self.dwells[sampled.mode].append(dwell)
            self.recovery[(sampled.mode, _bucket(dwell))].append(leaving - reaching)

    def report(self) -> dict[str, Any]:
        accuracy: dict[str, dict[str, Any]] = {}
        for (mode, horizon), (n, absolute, signed) in sorted(self.errors.items()):
            accuracy.setdefault(mode, {})[str(horizon)] = {
                "n": int(n),
                "mae_s": round(absolute / n, 1),
                "bias_s": round(signed / n, 1),
                "error_p": _quantiles(self.signed[(mode, horizon)]),
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
        by_where: dict[str, dict[str, Any]] = {}
        for (mode, where, age), (n, absolute, signed) in sorted(self.by_where.items()):
            by_where.setdefault(mode, {})[where if age == "all" else age] = {
                "n": int(n),
                "mae_s": round(absolute / n, 1),
                "bias_s": round(signed / n, 1),
            }
        return {
            "accuracy": accuracy,
            "next_call_by_position": by_where,
            "by_predicted_delay": by_delay,
            "flicker": flicker,
            "dwell": dwell,
            "off_shape": shape,
            "checks": self.checks.report(),
        }
