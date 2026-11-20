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


def _sampled(fixes: list[tuple[str, float]], log: QualityLog, run: Run) -> None:
    for clock, metres in fixes:
        run.at(clock, east(metres))
        estimate_all(run.state, run.ctx, local(f"2026-10-08 {clock}"))
        log.sample(run.state, run.ctx, local(f"2026-10-08 {clock}"))


def test_checks_pass_a_plain_run_and_score_its_departure_predictions() -> None:
    log, run = QualityLog(), Run()
    fixes: list[tuple[str, float]] = [
        ("08:00:00", 0),
        ("08:02:30", 500),
        ("08:04:40", 900),
        ("08:05:00", 1000),
        ("08:05:20", 1000),
        ("08:05:35", 1150),
        ("08:08:00", 1600),
        ("08:09:50", 1990),
        ("08:10:00", 2000),
    ]
    _sampled(fixes, log, run)
    checks = log.report()["checks"]
    events = checks["events"]["bus"]
    assert events["departures"] >= 1
    assert events["departed_while_there"] == events["stood_without_dwell"] == 0
    assert events["ended"] == events["ended_with_time"] == 1 and events["lost_near_end"] == 0
    assert checks["departure_accuracy"]["bus"]["1"]["n"] > 0


def test_checks_flag_a_departure_timed_while_the_fixes_are_still_at_the_stop() -> None:
    from dataclasses import replace

    log, run = QualityLog(), Run()
    _sampled(
        [
            ("08:00:00", 0),
            ("08:02:30", 500),
            ("08:04:40", 900),
            ("08:05:00", 1000),
            ("08:06:30", 1000),  # still at B a minute and a half on
            ("08:06:45", 1150),
        ],
        log,
        run,
    )
    # The core's own time for B's departure, replaced by one on its arrival.
    (journey,) = run.state.instances
    instance = run.state.instances[journey]
    b = instance.calls[1]
    assert b.passed_arrival is not None and b.passed_departure is not None
    calls = (instance.calls[0], replace(b, passed_departure=b.passed_arrival), *instance.calls[2:])
    run.state.instances[journey] = instance.evolve(calls=calls)
    log.sample(run.state, run.ctx, local("2026-10-08 08:10:00"))
    checks = log.report()["checks"]
    events = checks["events"]["bus"]
    assert events["departed_while_there"] == 1 and events["stood_without_dwell"] == 1
    assert checks["examples"]["stood_without_dwell"][0]["call"] == 1


def test_checks_count_a_journey_lost_just_short_of_its_last_stop() -> None:
    from dataclasses import replace

    log, run = QualityLog(), Run()
    _sampled([("08:00:00", 0), ("08:03:00", 600), ("08:06:00", 1200), ("08:09:00", 1700)], log, run)
    # Lost with no time at C (the core's own inference of that arrival is not run here).
    (journey,) = run.state.instances
    instance = run.state.instances[journey]
    lost = replace(instance.freshness, stale=True, lost=True)
    run.state.instances[journey] = replace(instance, freshness=lost)
    log.sample(run.state, run.ctx, local("2026-10-08 08:45:00"))
    report = log.report()["checks"]
    events = report["events"]["bus"]
    assert events["ended"] == 1 and events["ended_with_time"] == 0
    assert events["lost_near_end"] == 1
    assert 290 <= report["examples"]["lost_near_end"][0]["last_fix_from_end_m"] <= 310
