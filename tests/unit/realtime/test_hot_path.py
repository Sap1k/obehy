"""The tracker's fast paths agree exactly with the plain ones they replace."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from obehy.realtime.model import Freshness, Instance, JourneyKey, Lead, SourceTrack, VehicleId
from obehy.realtime.policy import load_policy
from obehy.realtime.timeline.plan import PlanCache
from obehy.realtime.times import (
    PRAGUE,
    candidate_service_dates,
    instant,
    local_date,
    micros,
    scaled_micros,
)
from tests.realtime.builder import ORIGIN, STEP_LON, timetable
from tests.realtime.observations import observe

POLICY = load_policy()


def test_microseconds_give_the_same_seconds_as_timedeltas() -> None:
    a = instant(datetime(2026, 10, 25, 0, 59, 59, 999_999, tzinfo=UTC))
    for delta in (timedelta(microseconds=1), timedelta(seconds=7, microseconds=3), timedelta(0)):
        b = instant(a + delta)
        assert (micros(b) - micros(a)) / 1_000_000 == (b - a).total_seconds()
        assert (micros(a) - micros(b)) / 1_000_000 == (a - b).total_seconds()
    for us in (1, 7, 600_000_001, -3_599_999_999):
        for factor in (0.0, 0.5, 1 / 3, 0.7071, 1.0):
            assert scaled_micros(us, factor) == (timedelta(microseconds=us) * factor) // timedelta(
                microseconds=1
            )


def test_cached_local_dates_across_both_dst_changes() -> None:
    for day in (date(2026, 3, 29), date(2026, 10, 25)):
        start = datetime.combine(day - timedelta(days=1), datetime.min.time(), UTC)
        for minutes in range(0, 3 * 24 * 60, 13):
            at = instant(start + timedelta(minutes=minutes, microseconds=999_999))
            local = at.astimezone(PRAGUE).date()
            assert local_date(at) == local
            assert candidate_service_dates(at) == (
                local - timedelta(days=1),
                local,
                local + timedelta(days=1),
            )


def test_lateness_in_microseconds_matches_the_timetable() -> None:
    day = date(2026, 10, 25)  # the autumn DST change: the 02:00-03:00 hour repeats
    tt = timetable().stop("A", ORIGIN[0], ORIGIN[1])
    tt.stop("B", ORIGIN[0] + STEP_LON, ORIGIN[1]).stop("C", ORIGIN[0] + 2 * STEP_LON, ORIGIN[1])
    index = tt.trip(
        "582492:143", days=[day], calls=[("A", "01:50"), ("B", "02:20"), ("C", "03:10")]
    ).index()
    (trip,) = index.trips.values()
    plan = PlanCache().plan(trip, day, index, POLICY)
    timetable_ = plan.timetable
    base = instant(datetime(2026, 10, 24, 23, 30, 0, 123_457, tzinfo=UTC))
    total = plan.path.distances_m[-1]
    for step in range(0, 200):
        at = instant(base + timedelta(seconds=97 * step, microseconds=step))
        for share in (0.0, 0.13, 0.25, 0.5, 0.77, 1.0, 1.2):
            along = -10.0 + share * total
            assert timetable_.lateness_us(micros(at), along) == timetable_.lateness_s(at, along)


def test_fact_lookup_by_type() -> None:
    observation = observe("2026-10-09 08:00", delay=60, position=(14.0, 50.0))
    for kind in {type(fact) for fact in observation.facts}:
        assert observation.first(kind) == next(f for f in observation.facts if type(f) is kind)
        assert observation.all(kind) == tuple(f for f in observation.facts if type(f) is kind)
    assert observation.first(Lead) is None
    assert observation.all(Lead) == ()


def test_evolve_equals_replace() -> None:
    at = instant(datetime(2026, 10, 9, 6, 0, tzinfo=UTC))
    instance = Instance(
        JourneyKey("jdf", "cis:line_trip", "582492:143", date(2026, 10, 9)),
        "run",
        "trip",
        "bus",
        "running",
        (),
        Freshness(at, at),
        delay_s=60,
        track_source="duk",
    )
    lead = Lead(VehicleId("duk", "1"), at)
    assert instance.evolve(lead=lead, delay_s=None) == replace(instance, lead=lead, delay_s=None)
    assert instance.evolve() == instance
    track = SourceTrack("duk", at, delay_s=5)
    assert track.evolve(position_at=at, delay_s=None) == replace(
        track, position_at=at, delay_s=None
    )
