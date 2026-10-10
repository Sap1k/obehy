"""Shadow evaluation of rail predictors during replay (docs/R2_SLICE.md section 11, item 5).

Every sample, for each running rail run with a source prediction, each predictor's arrival at
the predicted next stop is noted; once that arrival happens (an actual event), the errors are
scored. The policy's predictor is chosen from this report, never guessed. Pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from obehy.realtime.index import IndexView
from obehy.realtime.model import FeedState, Interval, JourneyKey
from obehy.realtime.policy import PREDICTORS
from obehy.realtime.timeline.fusion import anchor_prediction, current_lateness
from obehy.realtime.times import Instant, ServiceTime

Call = tuple[JourneyKey, int]


def _middle(interval: Interval) -> Instant:
    return Instant(interval.lo + (interval.hi - interval.lo) / 2)


@dataclass(slots=True)
class PredictorLog:
    # (run, call) -> predictor -> [(made at, predicted arrival)]
    predicted: dict[Call, dict[str, list[tuple[Instant, Instant]]]] = field(
        default_factory=dict[Call, dict[str, list[tuple[Instant, Instant]]]]
    )
    actual: dict[Call, Instant] = field(default_factory=dict[Call, Instant])

    def sample(self, state: FeedState, index: IndexView, at: Instant) -> None:
        for journey, instance in state.instances.items():
            if not instance.parts or instance.lifecycle != "running":
                continue
            trip = index.trip(instance.trip_id)
            for i, call in enumerate(instance.calls):
                if call.arrival is not None:
                    self.actual.setdefault((journey, i), _middle(call.arrival))
            own, _ = current_lateness(instance)
            for predictor in PREDICTORS:
                if predictor == "propagate":
                    target = anchor_prediction(instance, trip, index, "sz")
                    if target is None or own is None:
                        continue
                    lateness = own
                    i = target[0]
                else:
                    found = anchor_prediction(instance, trip, index, predictor)
                    if found is None:
                        continue
                    i, lateness = found
                scheduled = trip.calls[i].arrival
                if scheduled is None or (journey, i) in self.actual:
                    continue
                when = Instant(_scheduled(journey, scheduled) + lateness)
                self.predicted.setdefault((journey, i), {}).setdefault(predictor, []).append(
                    (at, when)
                )

    def report(self) -> dict[str, Any]:
        errors: dict[str, list[float]] = {name: [] for name in PREDICTORS}
        for call, by_predictor in self.predicted.items():
            actual = self.actual.get(call)
            if actual is None:
                continue
            for predictor, samples in by_predictor.items():
                for made, when in samples:
                    if made < actual:
                        errors[predictor].append((when - actual).total_seconds())
        out: dict[str, Any] = {}
        for name, values in errors.items():
            if not values:
                out[name] = {"n": 0}
                continue
            out[name] = {
                "n": len(values),
                "mae_s": round(sum(abs(v) for v in values) / len(values), 1),
                "bias_s": round(sum(values) / len(values), 1),
            }
        return out


def _scheduled(journey: JourneyKey, seconds: int) -> Instant:
    return ServiceTime(journey.service_date, seconds).instant()


SAMPLE_EVERY = timedelta(minutes=5)
