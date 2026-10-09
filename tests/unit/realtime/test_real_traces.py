"""Real závleky from the pinned DÚK corpus (docs/R1_SLICE.md section 9), as JSON fixtures."""

from __future__ import annotations

from datetime import datetime

import pytest

from obehy.realtime.core import Context, estimate_all, refresh, step
from obehy.realtime.model import CallState, Effect, FeedState, WriteEvent
from obehy.realtime.policy import load_policy
from obehy.realtime.times import PRAGUE, Instant, instant
from tests.realtime.traces import load

POLICY = load_policy()


def _replay(name: str) -> tuple[list[WriteEvent], FeedState]:
    index, observations = load(name)
    ctx = Context(index, POLICY)
    state = FeedState("jdf")
    effects: list[Effect] = []
    for observation in observations:
        effects.extend(step(state, observation, ctx))
    estimate_all(state, ctx)
    return [e for e in effects if isinstance(e, WriteEvent)], state


@pytest.mark.parametrize(
    ("name", "branch"),
    [
        # Hora Sv. Kateřiny: předávací stanice, pož. zbroj., nám., pož. zbroj., předávací stanice.
        ("duk-001521-104", [11, 12, 13, 14, 15]),
        # Kryštofovy Hamry: přehrada, Kryštofovy Hamry, přehrada; poor fit on the way in.
        ("duk-522586-103", [10, 11, 12]),
        ("duk-522586-107", [10, 11, 12]),
        ("duk-522586-111", [10, 11, 12]),
    ],
)
def test_a_real_zavlek_is_driven_in_visit_order(name: str, branch: list[int]) -> None:
    events, _ = _replay(name)
    index, _ = load(name)
    (trip,) = index.trips.values()
    by_call = {(e.location_id, e.visit_n, e.kind): e.interval for e in events}
    times: list[Instant] = []
    for call in trip.calls:
        if call.sequence not in branch:
            continue
        arrival = by_call.get((call.location_id, call.visit_n, "arrival"))
        departure = by_call.get((call.location_id, call.visit_n, "departure"))
        assert arrival is not None and departure is not None, f"call {call.sequence} missed"
        times += [arrival.hi, departure.hi]
    assert times == sorted(times)
    # Each visit of the branch takes real time: no two calls crossed by one jump.
    departures = times[1::2]
    assert len(set(departures)) == len(departures)


def test_real_event_times_follow_call_order() -> None:
    # 522591:112: two readings briefly disagree on when the bus left a stop; nothing may be
    # committed until they agree.
    for name in (
        "duk-001521-104",
        "duk-522586-103",
        "duk-522586-107",
        "duk-522586-111",
        "duk-522591-112",
    ):
        events, _ = _replay(name)
        times = [e.interval.hi for e in events]
        assert times == sorted(times), name


def _live(name: str, ticks: list[str]) -> dict[str, list[CallState]]:
    """What realtime showed at each tick (Prague wall-clock), from observations received by
    then: nothing later is known."""

    index, observations = load(name)
    ctx = Context(index, POLICY)
    state = FeedState("jdf")
    day = observations[0].received_at.astimezone(PRAGUE).date()
    shown: dict[str, list[CallState]] = {}
    pending = iter(observations)
    upcoming = next(pending, None)
    for clock in ticks:
        hour, minute = map(int, clock.split(":"))
        now = instant(datetime(day.year, day.month, day.day, hour, minute, tzinfo=PRAGUE))
        while upcoming is not None and upcoming.received_at <= now:
            step(state, upcoming, ctx)
            upcoming = next(pending, None)
        refresh(state, now, POLICY)
        estimate_all(state, ctx)
        (instance,) = state.instances.values()
        shown[clock] = list(instance.calls)
    return shown


def _clock(value: Instant | None) -> str | None:
    return None if value is None else value.astimezone(PRAGUE).strftime("%H:%M")


def test_a_bus_waiting_at_the_origin_with_a_frozen_gps_clock_is_shown_live_correctly() -> None:
    # 522586:107 waits at Chomutov,aut.nádr. (departure 11:05) with its GPS time frozen at
    # 11:00:01, leaves about 11:07:50, and loops north of the straight line to V Alejích.
    shown = _live("duk-522586-107", ["11:03", "11:08", "11:12"])
    origin, alejich, cernovicka = shown["11:03"][:3]
    assert _clock(origin.estimated_departure) == "11:05"  # waiting is not running early
    assert _clock(cernovicka.estimated_arrival) == "11:09"
    assert _clock(shown["11:08"][0].estimated_departure) == "11:07"  # leaving now, late
    origin, alejich, cernovicka = shown["11:12"][:3]
    assert (origin.status, alejich.status, cernovicka.status) == ("actual",) * 3
    assert _clock(origin.estimated_departure) == "11:07"
    assert _clock(alejich.estimated_arrival) == "11:09"
    assert _clock(cernovicka.estimated_arrival) == "11:10"


def test_a_reception_gap_keeps_predicting_and_is_corrected_afterwards() -> None:
    # No position from about 11:47 to 11:52 after the Kryštofovy Hamry branch.
    shown = _live("duk-522586-107", ["11:46", "11:50", "11:53"])
    for clock in ("11:46", "11:50"):
        last = shown[clock][14]
        assert last.status == "predicted"  # still in the feed, with the measured lateness
        assert _clock(last.estimated_arrival) == "11:54"  # planned 11:54, then ~2 min early
    potok, haj, prisecnicka, terminus = shown["11:53"][11:15]
    assert (potok.status, haj.status, prisecnicka.status) == ("inferred",) * 3
    assert terminus.status == "actual"
