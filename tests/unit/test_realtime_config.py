"""`realtime_config`: what the API, the release side and the jobs read of the realtime config.
Every shipped manifest and policy file loads through it (the Rust worker's loader is tested on
the Rust side, `rust/obehy-core/tests/config.rs`), and it reads only its own fields."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from obehy.realtime_config import (
    DATA,
    FEED_NAMES,
    POLICY,
    SOURCES,
    Applies,
    ConfigError,
    ServiceTime,
    TimeError,
    declared_lookups,
    instant,
    load_sources,
    local_date,
    parse_utc,
    read_policy,
    read_source,
    utc_text,
)

POLICIES = sorted(DATA.glob("policy-v*.toml"))
MANIFESTS = sorted(SOURCES.glob("*.toml"))


def test_the_shipped_files_are_found() -> None:
    assert POLICY in POLICIES
    assert len(MANIFESTS) >= 8


@pytest.mark.parametrize("path", POLICIES, ids=lambda p: p.name)
def test_every_policy_file_loads(path: Path) -> None:
    policy = read_policy(path)
    assert policy.journey_namespaces and len(set(policy.journey_namespaces)) == len(
        policy.journey_namespaces
    )
    assert policy.vehicle_day_gap_s > 0
    assert policy.retention.observation_hours > 0
    assert policy.retention.longest_raw_days >= policy.retention.raw_days("any-source") >= 1
    assert 0 < policy.guard.min_history_days <= policy.retention.history_days
    assert policy.guard.free_gb > 0 and policy.guard.min_raw_days > 0


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_every_manifest_loads(path: Path) -> None:
    source, lookups = read_source(path)
    assert source.source == path.stem
    assert source.title and source.covers
    assert set(source.live_feeds) <= set(FEED_NAMES)
    for lookup in lookups:
        assert lookup.source == source.source and lookup.kind and lookup.feeds


def test_sources_and_lookups_of_the_shipped_manifests() -> None:
    sources = {source.source: source for source in load_sources()}
    assert sources["duk"].live_feeds == ("jdf", "czptt")
    assert sources["mpvnet"].live_feeds == ("jdf",)  # trains recorded, not live
    lookups = declared_lookups()
    assert [lookup.kind for lookup in lookups] == ["composition"] * len(lookups)
    assert all(lookup.source in sources for lookup in lookups)


def test_only_its_own_fields_are_read(tmp_path: Path) -> None:
    manifest = tmp_path / "probe.toml"
    manifest.write_text(
        """
source = "probe"
manifest_version = 99
title = "Probe"
covers = "Recorded only."
something_new = { anything = true }

[[channel]]
name = "a"
feeds = ["jdf", "czptt"]
live_feeds = ["czptt"]
poll = { kind = "a kind the worker knows" }
[channel.decode]
reader = "whatever"

[[channel]]
name = "b"
feeds = ["jdf"]
live_feeds = []

[[lookup]]
name = "units"
kind = "composition"
feeds = ["czptt"]
applies = { keys = { "czptt:train_number" = "^1" }, days_around = 1 }
""",
        encoding="utf-8",
    )
    source, (lookup,) = read_source(manifest)
    assert source.live_feeds == ("czptt",)
    assert lookup.applies == Applies((("czptt:train_number", "^1"),), 1)
    on = date(2026, 11, 21)
    assert lookup.applies.refusal({"czptt:train_number": "1154"}, on, on) is None
    assert lookup.applies.refusal({"czptt:train_number": "2154"}, on, on) is not None
    assert lookup.applies.refusal({"czptt:train_number": "1154"}, date(2026, 11, 23), on)


def test_a_missing_or_wrong_value_it_reads_is_an_error(tmp_path: Path) -> None:
    manifest = tmp_path / "probe.toml"
    manifest.write_text('source = "probe"\ntitle = "Probe"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="covers"):
        read_source(manifest)
    manifest.write_text(
        'source = "probe"\ntitle = "P"\ncovers = "C"\n[[channel]]\nfeeds = ["gtfs"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="feeds"):
        read_source(manifest)
    policy = tmp_path / "policy.toml"
    policy.write_text(
        POLICY.read_text(encoding="utf-8").replace("free_gb = ", "free_gb_typo = "),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"guard\.free_gb"):
        read_policy(policy)


def test_prague_time_at_the_edge() -> None:
    # The autumn change: 02:30 is read as its first occurrence (CEST).
    assert ServiceTime(date(2026, 10, 25), 2 * 3600 + 1800).instant() == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )
    # Past 24:00 continues into the next day.
    assert ServiceTime(date(2026, 11, 20), 25 * 3600).instant() == datetime(
        2026, 11, 21, 0, 0, tzinfo=UTC
    )
    assert local_date(instant(datetime(2026, 11, 20, 23, 0, tzinfo=UTC))) == date(2026, 11, 21)
    assert local_date(instant(datetime(2026, 11, 20, 22, 59, tzinfo=UTC))) == date(2026, 11, 20)
    at = instant(datetime(2026, 11, 21, 7, 5, 3, 120, tzinfo=UTC))
    assert utc_text(at) == "2026-11-21T07:05:03.000120Z" and parse_utc(utc_text(at)) == at
    with pytest.raises(TimeError):
        instant(datetime(2026, 11, 21))  # noqa: DTZ001 - naive on purpose
