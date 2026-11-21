"""Shadow evaluation of rail predictors during replay (docs/R2_SLICE.md section 11, item 5).

Read from the output stream's `predictor_sample` records (`emit/samples.py`, every five minutes):
for each running rail run with a source prediction, each predictor's arrival at the predicted
next stop is noted; once that arrival happens (an actual event), the errors are scored. The
policy's predictor is chosen from this report, never guessed. Pure: it reads records, never the
core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from obehy.realtime.journey_view import Key, at, journey_key
from obehy.realtime.policy import PREDICTORS
from obehy.realtime.times import Instant, parse_utc

Call = tuple[Key, int]
Record = dict[str, Any]


@dataclass(slots=True)
class PredictorLog:
    # (run, call) -> predictor -> [(made at, predicted arrival)]
    predicted: dict[Call, dict[str, list[tuple[Instant, Instant]]]] = field(
        default_factory=dict[Call, dict[str, list[tuple[Instant, Instant]]]]
    )
    actual: dict[Call, Instant] = field(default_factory=dict[Call, Instant])

    def sample(self, record: Record) -> None:
        """One `predictor_sample` record."""

        made = parse_utc(record["at"])
        for item in cast(list[Record], record["journeys"]):
            journey = journey_key(item["journey"])
            for i, mid in item["arrivals"]:
                self.actual.setdefault((journey, i), parse_utc(mid))
            for predictor, i, when in item["predicted"]:
                if (journey, i) in self.actual:
                    continue
                arrival = at(when)
                assert arrival is not None
                self.predicted.setdefault((journey, i), {}).setdefault(predictor, []).append(
                    (made, arrival)
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
