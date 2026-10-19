"""The replay's prediction quality log (`realtime/quality.py`)."""

from __future__ import annotations

from obehy.realtime.core import estimate_all
from obehy.realtime.quality import QualityLog
from tests.realtime.observations import local
from tests.unit.realtime.test_timeline import Run, east


def test_quality_scores_predictions_and_measures_the_dwell_at_a_stop_without_one() -> None:
    log = QualityLog()
    run = Run()
    fixes = [
        ("08:00:00", 0),
        ("08:02:30", 500),
        ("08:04:40", 900),
        ("08:05:00", 1000),
        ("08:05:30", 1000),  # stands 30 s at B (08:05/08:05)
        ("08:05:45", 1100),
        ("08:08:00", 1600),
        ("08:10:00", 2000),
    ]
    for clock, metres in fixes:
        run.at(clock, east(metres))
        estimate_all(run.state, run.ctx, local(f"2026-10-08 {clock}"))
        log.sample(run.state, run.ctx, local(f"2026-10-08 {clock}"))
    report = log.report()
    accuracy = report["accuracy"]["bus"]
    assert accuracy["1"]["n"] > 0 and accuracy["1"]["mae_s"] < 60
    dwell = report["dwell"]["bus"]
    assert dwell["n"] == 1 and 20 <= dwell["p"]["50"] <= 50
    assert report["flicker"]["bus"]["calls"] == 2
