"""`obehy rt replay`: archived payloads through the live pipeline with a simulated clock.

Polls of the selected channels are merged in reception order, decoded by their connector and run
through the same `Runner` as the worker. Housekeeping ticks fall on fixed multiples of the emit
interval, so output never depends on when the replay runs. Outputs:

- `report.json`: decode counts, results by reason, and per DÚK fleet the share of running
  key groups (vehicle, trip key, local day) that bound to a journey;
- `gtfs-rt/<feed>/<UTC time>.pb` every `gtfs_rt_every_s` simulated seconds, if asked;
- with `write_history`, `rt.observation` and history rows for the replayed days (rebuilt).
"""

from __future__ import annotations

import heapq
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from pathlib import Path
from typing import Any

import psycopg

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.archive import ArchivedPoll, iter_polls
from obehy.realtime.core import CORE_VERSION, estimate_all
from obehy.realtime.emit.db import Writer, clear_observations
from obehy.realtime.emit.gtfs_rt import feed_message
from obehy.realtime.index_sql import ReleaseLoads, ensure_release
from obehy.realtime.model import (
    Derivation,
    Effect,
    Feed,
    Observation,
    ObservationResult,
    SourceState,
    TripKey,
    VehicleKey,
)
from obehy.realtime.policy import Policy
from obehy.realtime.runner import Runner
from obehy.realtime.sources import duk
from obehy.realtime.times import Instant, instant, local_date

REPORT_SCHEMA_VERSION = 2
LOOKAHEAD_POLLS = 40
SOURCES = ("duk",)

Decoder = Callable[[bytes, str, Instant, timedelta], list[Observation]]
DECODERS: dict[tuple[str, str], Decoder] = {(duk.SOURCE, duk.CHANNEL): duk.decode}


class ReplayError(RuntimeError):
    """The replay cannot run (no decoder for a channel, release not loadable)."""


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    release: str | Path
    archive: Path
    start: date
    end: date
    channels: Sequence[tuple[str, str]]
    out: Path
    feeds: tuple[Feed, ...] = ("jdf",)
    write_history: bool = False
    gtfs_rt_every_s: int | None = None
    since: Instant | None = None
    until: Instant | None = None


@dataclass(slots=True)
class _Stats:
    polls: int = 0
    failed_polls: int = 0
    bad_payloads: int = 0
    observations: int = 0


def _polls(options: ReplayOptions) -> Iterator[tuple[datetime, str, str, ArchivedPoll]]:
    streams = [
        (
            (poll.received_at, source, channel, poll)
            for poll in iter_polls(options.archive, source, channel, options.start, options.end)
        )
        for source, channel in sorted(options.channels)
    ]
    return heapq.merge(*streams, key=lambda item: (item[0], item[1], item[2]))


def _next_tick(at: datetime, every_s: int) -> Instant:
    seconds = math.floor(at.timestamp() / every_s + 1) * every_s
    return instant(datetime.fromtimestamp(seconds, UTC))


def replay(
    connection: psycopg.Connection,
    options: ReplayOptions,
    policy: Policy,
    *,
    runner: Runner | None = None,
    report: Callable[[str], None] = print,
) -> dict[str, Any]:
    for channel in options.channels:
        if channel not in DECODERS:
            raise ReplayError(f"no realtime connector decodes {channel[0]}/{channel[1]} yet")
    loads = ensure_release(connection, options.release, report=report)
    writer = _writer(connection, loads, options, policy) if options.write_history else None
    if runner is None:
        runner = Runner(connection, loads, policy, options.feeds, writer)
    else:
        runner.writer = writer
    stats: dict[str, _Stats] = defaultdict(_Stats)
    groups: dict[tuple[str, str, Any, date], bool] = {}
    reasons: Counter[str] = Counter()
    tick_s = policy.emit_tick_s
    next_tick: Instant | None = None
    next_snapshot: Instant | None = None
    last: Instant | None = None

    for chunk in batched(_decoded(options, policy, stats), LOOKAHEAD_POLLS, strict=False):
        # Load the static data of the next polls' keys in one round trip set.
        runner.prefetch([o for _, observations in chunk for o in observations or ()])
        for at, observations in chunk:
            if next_tick is None:
                next_tick = _next_tick(at, tick_s)
                if options.gtfs_rt_every_s:
                    next_snapshot = _next_tick(at, options.gtfs_rt_every_s)
            while next_tick <= at:
                runner.tick(next_tick, emit=False)
                if next_snapshot is not None and next_snapshot <= next_tick:
                    _snapshot(options, runner, next_snapshot)
                    next_snapshot = instant(
                        next_snapshot + timedelta(seconds=options.gtfs_rt_every_s or 0)
                    )
                next_tick = instant(next_tick + timedelta(seconds=tick_s))
            if observations is None:
                continue
            _count(runner.process(observations), groups, reasons)
            last = at
    if last is not None:
        runner.tick(last, emit=False)
        for runtime in runner.runtimes.values():
            estimate_all(runtime.state, runtime.ctx)
        if writer is not None:
            writer.write_state({feed: r.state for feed, r in runner.runtimes.items()})
    document = _report(loads, options, stats, groups, reasons, runner.skipped)
    options.out.mkdir(parents=True, exist_ok=True)
    with atomic_output_path(options.out / "report.json") as temporary:
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    return document


def _decoded(
    options: ReplayOptions, policy: Policy, stats: dict[str, _Stats]
) -> Iterator[tuple[Instant, list[Observation] | None]]:
    """Each poll in the window, decoded; None for a failed or undecodable poll."""

    for received, source, channel, poll in _polls(options):
        at = instant(received)
        if options.until is not None and at >= options.until:
            return
        if options.since is not None and at < options.since:
            continue
        name = f"{source}/{channel}"
        stats[name].polls += 1
        body = poll.body()
        status = poll.entry.get("status")
        if body is None or poll.entry.get("error") or not isinstance(status, int) or status >= 300:
            stats[name].failed_polls += 1
            yield at, None
            continue
        try:
            observations = DECODERS[(source, channel)](
                body, str(poll.entry["sha256"]), at, policy.time.max_clock_skew
            )
        except ValueError:
            stats[name].bad_payloads += 1
            yield at, None
            continue
        stats[name].observations += len(observations)
        yield at, observations


def _writer(
    connection: psycopg.Connection, loads: ReleaseLoads, options: ReplayOptions, policy: Policy
) -> Writer:
    writer = Writer(connection, Derivation(CORE_VERSION, policy.version, loads.run_id))
    first = datetime(options.start.year, options.start.month, options.start.day, tzinfo=UTC)
    last = datetime(options.end.year, options.end.month, options.end.day, tzinfo=UTC)
    clear_observations(
        connection,
        first,
        last + timedelta(days=1),
        sorted({source for source, _ in options.channels}),
    )
    days = [
        options.start + timedelta(days=n) for n in range((options.end - options.start).days + 1)
    ]
    writer.clear_history(days)
    return writer


def _snapshot(options: ReplayOptions, runner: Runner, at: Instant) -> None:
    for feed, runtime in sorted(runner.runtimes.items()):
        estimate_all(runtime.state, runtime.ctx)
        path = options.out / "gtfs-rt" / feed / f"{at:%Y%m%dT%H%M%SZ}.pb"
        path.parent.mkdir(parents=True, exist_ok=True)
        message = feed_message(runtime.state, at)
        with atomic_output_path(path) as temporary:
            temporary.write_bytes(message.SerializeToString(deterministic=True))


def _count(
    effects: Sequence[Effect],
    groups: dict[tuple[str, str, Any, date], bool],
    reasons: Counter[str],
) -> None:
    for effect in effects:
        if not isinstance(effect, ObservationResult):
            continue
        obs = effect.observation
        reasons[f"{obs.feed}/{'bound' if effect.journey else effect.reason}"] += 1
        vehicle, key = obs.first(VehicleKey), obs.first(TripKey)
        state = obs.first(SourceState)
        if vehicle is None or key is None or (state is not None and state.code != "running"):
            continue
        group = (obs.source, vehicle.source_vehicle_id, key, local_date(obs.at))
        groups[group] = groups.get(group, False) or effect.journey is not None


def _report(
    loads: ReleaseLoads,
    options: ReplayOptions,
    stats: dict[str, _Stats],
    groups: dict[tuple[str, str, Any, date], bool],
    reasons: Counter[str],
    skipped: int,
) -> dict[str, Any]:
    fleets: dict[str, Counter[str]] = defaultdict(Counter)
    for (source, vehicle, key, _), bound in groups.items():
        feed: Feed = "czptt" if key.namespace.startswith("czptt:") else "jdf"
        name = f"{source}/{duk.fleet(vehicle, feed)}" if source == duk.SOURCE else source
        fleets[name]["key_groups"] += 1
        fleets[name]["bound"] += int(bound)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "core_version": CORE_VERSION,
        "release": {"run_id": loads.run_id},
        "archive": {
            "from": options.start.isoformat(),
            "to": options.end.isoformat(),
            "channels": {
                name: {
                    "polls": s.polls,
                    "failed_polls": s.failed_polls,
                    "bad_payloads": s.bad_payloads,
                    "observations": s.observations,
                }
                for name, s in sorted(stats.items())
            },
        },
        "feeds": list(options.feeds),
        "skipped_other_feeds": skipped,
        "results": dict(sorted(reasons.items())),
        "running_key_groups": {
            name: dict(sorted(counter.items())) for name, counter in sorted(fleets.items())
        },
    }


def summary_lines(document: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for name, counter in document["running_key_groups"].items():
        total, bound = counter.get("key_groups", 0), counter.get("bound", 0)
        share = 100 * bound / total if total else 0.0
        lines.append(f"{name}: {bound}/{total} running key groups bound ({share:.1f}%)")
    return lines
