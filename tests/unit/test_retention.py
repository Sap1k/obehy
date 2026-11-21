"""Retention and the disk guard (`obehy jobs`): what the jobs read of the archive, the
partitions and the policy. They stay Python after the switch to the Rust worker."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from obehy.realtime.disk_guard import GB, Step, next_step
from obehy.realtime.jobs import archive_sources, history_day, observation_end, prune_archive
from obehy.realtime_config import Guard, policy_values

GUARD = Guard(free_gb=6, min_raw_days=3, min_history_days=7)
RAW = [date(2026, 10, d) for d in range(10, 24)]
HISTORY = [date(2026, 10, d) for d in range(2, 24)]


def test_observation_partitions_end_after_their_utc_hour() -> None:
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
    policy = policy_values()
    assert policy.retention.history_days == 21
    assert policy.retention.raw_days("pid") == 1
    assert policy.retention.raw_days("sz-tabule") == 14
    assert policy.retention.longest_raw_days == 14
    assert policy.guard.min_history_days <= policy.retention.history_days


def test_the_archive_is_pruned_per_source(tmp_path: Path) -> None:
    for source, channel in (("pid", "vehicles"), ("sz-tabule", "boards")):
        for day in (20, 21, 22):
            (tmp_path / source / channel / f"2026-10-{day}").mkdir(parents=True)
    assert archive_sources(tmp_path) == ["pid", "sz-tabule"]
    pruned = prune_archive(tmp_path, date(2026, 10, 22), "pid")
    assert [p.name for p in pruned] == ["2026-10-20", "2026-10-21"]
    assert sorted(p.name for p in (tmp_path / "sz-tabule" / "boards").iterdir()) == [
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
    ]
