"""DÚK GPS and the SŽ map on one run (docs/R2_SLICE.md section 5, scenarios R6-R15, SZ-Q3/Q4/Q6)."""

from __future__ import annotations

from datetime import date, timedelta

from obehy.realtime.core import Context, estimate_all, refresh, step
from obehy.realtime.index import Index
from obehy.realtime.manifest import semantics_by_channel
from obehy.realtime.model import (
    Delay,
    Effect,
    Fact,
    FeedState,
    Instance,
    Interval,
    JourneyKey,
    NextPoint,
    NextStopPrediction,
    Observation,
    PointEvent,
    Position,
    RawRef,
    ServiceDay,
    TripKey,
    VehicleKey,
    WriteEvent,
)
from obehy.realtime.policy import load_policy
from obehy.realtime.record import load_channels
from obehy.realtime.timeline.fusion import merge
from tests.realtime.builder import ORIGIN, STEP_LON, timetable
from tests.realtime.observations import local

POLICY = load_policy()
SEMANTICS = semantics_by_channel(load_channels())
D8 = date(2026, 10, 8)
PA = JourneyKey("czptt", "czptt:pa", "PA1", D8)


def _index() -> Index:
    """A ~X B C on one run, train 100; SR70 codes 10001 (A), 10009 (X), 10002 (B), 10003 (C)."""

    tt = timetable("czptt", "czptt:train_number")
    for n, (name, code) in enumerate(
        [("A", "10001"), ("X", "10009"), ("B", "10002"), ("C", "10003")]
    ):
        tt.stop(name, ORIGIN[0] + n * STEP_LON, ORIGIN[1], sr70=code)
    tt.trip(
        "100",
        trip_id="czptt:trip:PA1:1",
        days=[D8],
        calls=[("A", "08:00"), ("~X", "08:05"), ("B", "08:10", "08:12"), ("C", "08:20")],
        run=("PA1", 1),
        train="100",
        mode="rail",
        shape=False,
    )
    tt.key("czptt:tr", "Tr:1", "czptt:trip:PA1:1")
    return tt.index()


def _sz(at: str, *facts: Fact) -> Observation:
    when = local(at)
    base: tuple[Fact, ...] = (VehicleKey("TR/1"), TripKey("czptt:tr", "Tr:1"), ServiceDay(D8))
    return Observation(
        "sz-mapa", "trains", "czptt", when, when, RawRef("1" * 64, 0), 1, base + facts
    )


def _duk(at: str, x: float) -> Observation:
    when = local(at)
    facts: tuple[Fact, ...] = (
        VehicleKey("950"),
        TripKey("czptt:train_number", "100"),
        Position(lat=ORIGIN[1], lon=ORIGIN[0] + x * STEP_LON),
    )
    return Observation("duk", "vehicles", "czptt", when, when, RawRef("2" * 64, 0), 1, facts)


def _point(name: str, codes: tuple[str, ...], at: str, *, standing: bool = False) -> PointEvent:
    when = local(at)
    return PointEvent(name, codes, None, Interval(when, when + timedelta(seconds=59)), standing)


def _run(index: Index, *observations: Observation) -> tuple[FeedState, list[Effect], Context]:
    state = FeedState("czptt")
    ctx = Context(index, POLICY, semantics=SEMANTICS)
    effects: list[Effect] = []
    for observation in observations:
        effects.extend(step(state, observation, ctx))
    return state, effects, ctx


def _events(effects: list[Effect]) -> list[tuple[str, str, str]]:
    return [(e.location_id, e.kind, e.method) for e in effects if isinstance(e, WriteEvent)]


def test_sz_q3_standing_is_the_arrival_and_leaving_the_departure() -> None:
    state, effects, _ = _run(
        _index(),
        _sz("2026-10-08 08:11:10", _point("B", ("10002",), "2026-10-08 08:11", standing=True)),
        _sz("2026-10-08 08:13:10", _point("B", ("10002",), "2026-10-08 08:13")),
    )
    assert _events(effects) == [
        ("czptt:B", "arrival", "source"),
        ("czptt:B", "departure", "source"),
    ]
    b = state.instances[PA].calls[2]
    assert b.arrival == Interval(local("2026-10-08 08:11"), local("2026-10-08 08:11:59"))
    assert state.instances[PA].lifecycle == "running"


def test_sz_q3_a_railway_point_is_a_passage() -> None:
    _, effects, _ = _run(
        _index(), _sz("2026-10-08 08:06", _point("X", ("10009",), "2026-10-08 08:05"))
    )
    assert _events(effects) == [("czptt:X", "passage", "source")]


def test_sz_q4_the_run_decides_between_codes_of_one_name() -> None:
    _, effects, _ = _run(
        _index(), _sz("2026-10-08 08:13", _point("B", ("99999", "10002"), "2026-10-08 08:13"))
    )
    assert _events(effects) == [("czptt:B", "departure", "source")]


def test_sz_q4_the_previous_next_point_names_an_unknown_point() -> None:
    _, effects, _ = _run(
        _index(),
        _sz("2026-10-08 08:06", NextPoint("B", "10002")),
        _sz("2026-10-08 08:13", _point("B", (), "2026-10-08 08:13")),
    )
    assert _events(effects) == [("czptt:B", "departure", "source")]


def test_an_unresolved_point_moves_nothing() -> None:
    state, effects, _ = _run(
        _index(), _sz("2026-10-08 08:13", _point("Nowhere", (), "2026-10-08 08:13"))
    )
    assert _events(effects) == [] and state.instances[PA].progress is None


def test_sz_q6_an_unchanged_entry_is_no_news() -> None:
    same = _sz("2026-10-08 08:06", Delay(60, "point"))
    state, _, ctx = _run(_index(), same)
    heard = state.instances[PA].freshness.heard_at
    later = Observation(
        same.source,
        same.channel,
        same.feed,
        local("2026-10-08 08:20"),
        same.observed_at,
        same.raw,
        1,
        same.facts,
    )
    step(state, later, ctx)
    assert state.instances[PA].freshness.heard_at == heard


def test_r7_a_point_event_anchors_progress_in_a_gps_gap() -> None:
    state, _, _ = _run(
        _index(),
        _duk("2026-10-08 08:00:30", 0.2),
        _duk("2026-10-08 08:02:00", 0.5),
        _sz("2026-10-08 08:13:10", _point("B", ("10002",), "2026-10-08 08:13")),
    )
    instance = state.instances[PA]
    assert instance.progress is not None and instance.progress.call_index == 2
    assert instance.track is not None
    assert all(
        h.along_m >= instance.track.committed_m - POLICY.progress.jitter_m
        for h in instance.track.hypotheses
    )


def test_r14_fresh_gps_leads_the_position() -> None:
    sz_fix = Position(lat=ORIGIN[1], lon=ORIGIN[0] + 0.9 * STEP_LON)
    state, _, _ = _run(
        _index(),
        _duk("2026-10-08 08:00:30", 0.2),
        _duk("2026-10-08 08:01:30", 0.4),
        _sz("2026-10-08 08:02:00", sz_fix),
    )
    assert state.instances[PA].track_source == "duk"
    later, _, _ = _run(
        _index(),
        _duk("2026-10-08 08:00:30", 0.2),
        _sz("2026-10-08 08:05:00", sz_fix),  # DÚK silent for longer than position_fresh_s
    )
    assert later.instances[PA].track_source == "sz-mapa"


def test_r6_a_run_is_stale_only_when_every_source_is_silent() -> None:
    state, _, _ = _run(
        _index(),
        _duk("2026-10-08 08:00:30", 0.2),
        _sz("2026-10-08 08:20:00", Delay(120, "point")),
    )
    refresh(state, local("2026-10-08 08:30"), POLICY)
    assert not state.instances[PA].freshness.stale  # DÚK left; SŽ still reports
    refresh(state, local("2026-10-08 08:40"), POLICY)
    assert state.instances[PA].freshness.stale


def test_r15_a_crossing_inside_the_point_minute_narrows_it_one_outside_loses() -> None:
    point = Interval(local("2026-10-08 08:11"), local("2026-10-08 08:11:59"))
    inside = Interval(local("2026-10-08 08:11:20"), local("2026-10-08 08:12:30"))
    outside = Interval(local("2026-10-08 08:15"), local("2026-10-08 08:15:30"))
    assert merge(point, inside, new_is_point=False, policy=POLICY) == Interval(
        local("2026-10-08 08:11:20"), local("2026-10-08 08:12:30")
    )
    assert merge(point, outside, new_is_point=False, policy=POLICY) == point
    assert merge(inside, point, new_is_point=True, policy=POLICY) == Interval(
        local("2026-10-08 08:11:20"), local("2026-10-08 08:12:30")
    )


def _predicted_c(predictor: str) -> object:
    from dataclasses import replace

    index = _index()
    state, _, ctx = _run(
        index,
        _sz(
            "2026-10-08 08:13:10",
            _point("B", ("10002",), "2026-10-08 08:13"),
            Delay(60, "point"),
            NextStopPrediction("10003", local("2026-10-08 08:20"), local("2026-10-08 08:24")),
        ),
    )
    policy = replace(POLICY, rail=replace(POLICY.rail, predictor=predictor))
    estimate_all(state, Context(index, policy, ctx.plans, ctx.semantics))
    instance: Instance = state.instances[PA]
    return instance.calls[3].estimated_arrival


def test_predictors_anchor_change_sz_and_propagate() -> None:
    # SŽ: 1 min late now, 4 min late at C; own anchor here is SŽ's point delay too (no GPS).
    assert _predicted_c("anchor_change") == local("2026-10-08 08:24")
    assert _predicted_c("sz") == local("2026-10-08 08:24")
    assert _predicted_c("propagate") == local("2026-10-08 08:21")


def test_sz_q10_a_repeated_point_changes_nothing_and_a_late_one_still_places() -> None:
    point = _point("B", ("10002",), "2026-10-08 08:13")
    _, effects, _ = _run(
        _index(),
        _sz("2026-10-08 08:13:10", point, Delay(60, "point")),
        _sz("2026-10-08 08:13:40", point, Delay(120, "point")),  # same cna, other fields changed
    )
    assert _events(effects) == [("czptt:B", "departure", "source")]
    _, late, _ = _run(
        _index(),
        _duk("2026-10-08 08:10:30", 2.0),
        _duk("2026-10-08 08:14:00", 2.4),
        _duk("2026-10-08 08:15:00", 2.6),
        _sz("2026-10-08 08:15:10", _point("B", ("10002",), "2026-10-08 08:13")),
    )
    assert ("czptt:B", "departure", "source") in _events(late)
