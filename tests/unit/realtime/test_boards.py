"""SŽ station boards: connector (SZT quirks), platforms in the core (R10-R12) and demand reads."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from obehy.realtime.archive import Poll
from obehy.realtime.core import Context, estimate_all, step
from obehy.realtime.emit.gtfs_rt import feed_message
from obehy.realtime.index import Index
from obehy.realtime.manifest import Channel
from obehy.realtime.model import (
    FeedState,
    JourneyKey,
    Observation,
    PlatformAssignment,
    RawRef,
    RecordPlatform,
    TripKey,
    Unresolved,
)
from obehy.realtime.policy import load_policy
from obehy.realtime.runtime.demand import Pace, ReadLog, plan_reads, run_demand
from obehy.realtime.sources import sz_tabule
from tests.realtime.builder import timetable
from tests.realtime.observations import local

POLICY = load_policy()
# Planner tests use their own refresh intervals, not the shipped ones.
PLAN = replace(
    POLICY.boards,
    early_min=45,
    near_min=15,
    near_refresh_s=300,
    imminent_min=5,
    imminent_refresh_s=120,
)
BOARDS = Path(__file__).resolve().parents[2] / "realtime" / "boards"
SKEW = timedelta(minutes=20)
D8 = date(2026, 10, 8)
PA = JourneyKey("czptt", "czptt:pa", "PA1", D8)


# --- connector -----------------------------------------------------------------------------------


def _board(name: str) -> bytes:
    return (BOARDS / name).read_bytes()


def test_szt_q2_kolin_numbers_platforms_with_both_tables() -> None:
    observations = sz_tabule.decode(
        _board("kolin-534149.html"), "a" * 64, local("2026-10-10 13:36"), SKEW, {"sr70": "534149"}
    )
    facts = [o.first(PlatformAssignment) for o in observations]
    assert len(observations) == 22
    assert all(f is not None and f.label == "platform" and f.station == "53414" for f in facts)
    first = observations[0]
    assert first.first(TripKey) == TripKey("czptt:train_number", "116")
    assert facts[0] == PlatformAssignment(
        "53414", "arrival", local("2026-10-10 13:09"), "2", "platform"
    )
    assert {f.kind for f in facts if f is not None} == {"arrival", "departure"}
    assert first.first(TripKey) is not None and first.observed_at is None


def test_szt_q2_q4_a_small_station_numbers_tracks_and_dashes_are_no_assignment() -> None:
    body = _board("ponikla-571500.html")
    assert (
        sz_tabule.decode(body, "a" * 64, local("2026-10-10 13:36"), SKEW, {"sr70": "571500"}) == []
    )
    assigned = body.replace(b'inline-block">-</span>', b'inline-block">1</span>', 1)
    (observation,) = sz_tabule.decode(
        assigned, "a" * 64, local("2026-10-10 13:36"), SKEW, {"sr70": "571500"}
    )
    fact = observation.first(PlatformAssignment)
    assert fact is not None and (fact.station, fact.value, fact.label) == ("57150", "1", "track")


def test_szt_q1_a_poll_needs_its_six_digit_station() -> None:
    try:
        sz_tabule.decode(_board("kolin-534149.html"), "a" * 64, local("2026-10-10 13:36"), SKEW, {})
    except sz_tabule.PayloadError:
        return
    raise AssertionError("a board without its station must not decode")


# --- platforms in the core -----------------------------------------------------------------------


def _index() -> Index:
    tt = timetable("czptt", "czptt:train_number")
    tt.stop("A", 14.0, 50.0, sr70="10001").stop("B", 14.014, 50.0, sr70="10002")
    tt.track("A", "1").track("A", "2")
    tt.trip(
        "100",
        trip_id="czptt:trip:PA1:1",
        days=[D8],
        calls=[("A", "08:00"), ("B", "08:10")],
        run=("PA1", 1),
        train="100",
        mode="rail",
        shape=False,
    )
    return tt.index()


def _row(
    value: str, label: str = "track", *, at: str = "2026-10-08 07:40", when: str = "08:00"
) -> Observation:
    received = local(at)
    fact = PlatformAssignment(
        "10001",
        "departure",
        local(f"2026-10-08 {when}"),
        value,
        "track" if label == "track" else "platform",
    )
    facts = (TripKey("czptt:train_number", "100"), fact)
    return Observation("sz-tabule", "board", "czptt", received, None, RawRef("3" * 64, 0), 1, facts)


def test_r10_a_board_row_gives_a_train_without_an_instance_its_track() -> None:
    index = _index()
    state = FeedState("czptt")
    effects = step(state, _row("2"), Context(index, POLICY))
    assert state.vehicles == {}
    call = state.instances[PA].calls[0]
    assert call.platform is not None
    assert (call.platform.value, call.platform.boarding_point_id) == ("2", "czptt:A:platform:2")
    (record,) = [e for e in effects if isinstance(e, RecordPlatform)]
    assert record.journey == JourneyKey("czptt", "czptt:train_number", "100", D8)
    estimate_all(state, Context(index, POLICY))
    message = feed_message(state, local("2026-10-08 07:41"))
    (update,) = [e.trip_update for e in message.entity if e.HasField("trip_update")]
    (stop,) = update.stop_time_update
    assert stop.stop_time_properties.assigned_stop_id == "czptt:A:platform:2"
    assert not stop.HasField("departure")  # no times without realtime


def test_r11_a_newer_assignment_replaces_the_older() -> None:
    index = _index()
    state = FeedState("czptt")
    ctx = Context(index, POLICY)
    step(state, _row("2"), ctx)
    step(state, _row("1", at="2026-10-08 07:50"), ctx)
    platform = state.instances[PA].calls[0].platform
    assert platform is not None and platform.value == "1"


def test_r12_a_platform_number_is_a_label_only() -> None:
    index = _index()
    state = FeedState("czptt")
    step(state, _row("3", "platform"), Context(index, POLICY))
    platform = state.instances[PA].calls[0].platform
    assert platform is not None and platform.boarding_point_id is None
    estimate_all(state, Context(index, POLICY))
    message = feed_message(state, local("2026-10-08 07:41"))
    assert not [e for e in message.entity if e.HasField("trip_update")]


def test_a_row_matching_no_call_is_reported() -> None:
    state = FeedState("czptt")
    effects = step(state, _row("2", when="08:03"), Context(_index(), POLICY))
    assert [e.kind for e in effects if isinstance(e, Unresolved)] == ["platform"]


# --- demand reads --------------------------------------------------------------------------------


def test_plan_reads_once_early_then_refreshes_near_and_imminent() -> None:
    policy = PLAN
    now = local("2026-10-08 08:00")
    due = {"100011": [local("2026-10-08 08:30")], "100029": [local("2026-10-08 09:30")]}
    log = ReadLog()
    assert plan_reads(now, due, log, policy, 60) == ["100011"]  # 09:30 is beyond early_min
    log.last["100011"] = now
    assert plan_reads(local("2026-10-08 08:10"), due, log, policy, 60) == []
    assert plan_reads(local("2026-10-08 08:16"), due, log, policy, 60) == ["100011"]  # near
    log.last["100011"] = local("2026-10-08 08:22")
    assert plan_reads(local("2026-10-08 08:25"), due, log, policy, 60) == ["100011"]  # imminent


def test_plan_reads_puts_unread_stations_first_and_drops_over_the_budget() -> None:
    now = local("2026-10-08 08:00")
    due = {
        "1": [local("2026-10-08 08:20")],
        "2": [local("2026-10-08 08:03")],
        "3": [local("2026-10-08 08:10")],
    }
    log = ReadLog(last={"2": local("2026-10-08 07:55")})
    # 2 was read 5 min ago and is due in 3 (imminent): a refresh, after the unread ones.
    assert plan_reads(now, due, log, PLAN, 2) == ["3", "1"]
    assert log.dropped == 1
    log2 = ReadLog()
    assert plan_reads(now, due, log2, PLAN, 1) == ["2"]
    assert log2.dropped == 2


def test_a_refusal_halves_the_budget() -> None:
    pace = Pace(POLICY.boards)
    now = local("2026-10-08 08:00")
    assert pace.budget(now) == POLICY.boards.ceiling_per_min
    pace.note(Poll(now, now, 429, None, error="HTTP 429"), now)
    assert pace.budget(now) == POLICY.boards.ceiling_per_min // 2
    assert (
        pace.budget(now + timedelta(seconds=POLICY.boards.slowdown_s))
        == POLICY.boards.ceiling_per_min
    )


def test_run_demand_fills_the_station_into_the_body_and_keeps_it_with_the_poll() -> None:
    channel = Channel(
        "sz-tabule",
        "board",
        "POST",
        "http://x.invalid",
        60.0,
        5.0,
        body=b"SR70={sr70}&x=1",
        demand="station_boards",
    )
    sent: list[bytes] = []
    polls: list[Poll] = []
    now = local("2026-10-08 08:00")

    def fetch(_: Channel, body: bytes) -> Poll:
        sent.append(body)
        return Poll(now, now, 200, b"<html/>")

    async def on_poll(_: Channel, poll: Poll) -> None:
        polls.append(poll)

    asyncio.run(
        run_demand(
            channel,
            asyncio.Event(),
            lambda _: {"534149": [local("2026-10-08 08:10")]},
            fetch,
            on_poll,
            POLICY.boards,
            clock=lambda: now,
            once=True,
        )
    )
    assert sent == [b"SR70=534149&x=1"]
    assert [p.request for p in polls] == [{"sr70": "534149"}]
