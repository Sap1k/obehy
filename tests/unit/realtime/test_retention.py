from __future__ import annotations

from datetime import UTC, date, datetime

from obehy.realtime.disk_guard import GB, Step, next_step
from obehy.realtime.emit.storage import (
    history_day,
    hour_of,
    observation_end,
    observation_name,
)
from obehy.realtime.policy import GuardPolicy, load_policy

GUARD = GuardPolicy(free_gb=6, min_raw_days=3, min_history_days=7)
RAW = [date(2026, 10, d) for d in range(10, 24)]
HISTORY = [date(2026, 10, d) for d in range(2, 24)]


def test_observation_partitions_are_named_by_utc_hour() -> None:
    hour = hour_of(datetime(2026, 10, 24, 1, 59, 30, tzinfo=UTC))
    assert observation_name(hour) == "observation_2026102401"
    assert observation_end("observation_2026102401") == datetime(2026, 10, 24, 2, tzinfo=UTC)
    assert observation_end("observation_2026102423") == datetime(2026, 10, 25, tzinfo=UTC)
    # An older server's daily partition ends at the next midnight UTC.
    assert observation_end("observation_20261023") == datetime(2026, 10, 24, tzinfo=UTC)
    assert observation_end("observation_time") is None


def test_history_partitions_are_named_by_service_date() -> None:
    assert history_day("actual_stop_event_20261023") == date(2026, 10, 23)
    assert history_day("journey_20261001") == date(2026, 10, 1)
    assert history_day("journey_202610") is None
    assert history_day("unknown_20261023") is None


def test_the_guard_does_nothing_with_room_enough() -> None:
    assert next_step(6 * GB, GUARD, RAW, HISTORY) is None


def test_the_guard_deletes_the_oldest_raw_day_first() -> None:
    assert next_step(GB, GUARD, RAW, HISTORY) == Step("raw", date(2026, 10, 10))


def test_the_guard_deletes_history_once_raw_is_at_its_minimum() -> None:
    assert next_step(GB, GUARD, RAW[-3:], HISTORY) == Step("history", date(2026, 10, 2))


def test_the_guard_stops_at_both_minimums() -> None:
    assert next_step(0, GUARD, RAW[-3:], HISTORY[-7:]) is None


def test_the_policy_window_fits_the_api() -> None:
    policy = load_policy()
    assert policy.retention.history_days == 21
    assert policy.retention.observation_hours > policy.warm_replay_max_hours
    assert policy.guard.min_history_days <= policy.retention.history_days
