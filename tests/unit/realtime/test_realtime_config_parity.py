"""Until the switch to the Rust worker: `realtime_config` reads the shipped files as the Python
worker's loaders do (`policy.py`, `manifest.py`), and the jobs read the partition names the
Python writer gives. Deleted with the Python worker."""

from __future__ import annotations

from datetime import UTC, datetime

from obehy.realtime import record, times
from obehy.realtime.emit.storage import hour_of, observation_name
from obehy.realtime.jobs import observation_end
from obehy.realtime.manifest import declared_lookups as worker_lookups
from obehy.realtime.policy import load_policy
from obehy.realtime_config import declared_lookups, load_sources, policy_values


def test_the_policy_values_are_the_workers() -> None:
    worker, read = load_policy(), policy_values()
    assert read.journey_namespaces == worker.journey.namespaces
    assert read.vehicle_day_gap_s == worker.time.vehicle_day_gap_s
    assert read.guard.free_gb == worker.guard.free_gb
    assert read.guard.min_raw_days == worker.guard.min_raw_days
    assert read.guard.min_history_days == worker.guard.min_history_days
    assert read.retention.observation_hours == worker.retention.observation_hours
    assert read.retention.history_days == worker.retention.history_days
    assert read.retention.longest_raw_days == worker.retention.longest_raw_days
    for source in ("pid", "sz-tabule", "unknown"):
        assert read.retention.raw_days(source) == int(worker.retention.raw_days(source))
    # Warm start replays observations the hourly job has not dropped yet.
    assert read.retention.observation_hours > worker.warm_replay_max_hours


def test_the_sources_and_lookups_are_the_workers() -> None:
    worker = record.load_sources()
    assert [(s.source, s.title, s.covers, s.live_feeds) for s in load_sources()] == [
        (s.source, s.title, s.covers, s.live_feeds) for s in worker
    ]
    assert [(lk.source, lk.name, lk.kind, lk.feeds, lk.applies) for lk in declared_lookups()] == [
        (lk.source, lk.name, lk.kind, lk.feeds, lk.applies) for lk in worker_lookups()
    ]


def test_the_jobs_read_the_observation_partitions_the_writer_names() -> None:
    hour = hour_of(datetime(2026, 10, 24, 1, 59, 30, tzinfo=UTC))
    assert observation_name(hour) == "observation_2026102401"
    assert observation_end(observation_name(hour)) == datetime(2026, 10, 24, 2, tzinfo=UTC)


def test_the_workers_times_are_the_edges() -> None:
    at = times.instant(datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
    assert times.candidate_service_dates(at)[1] == times.local_date(at)
