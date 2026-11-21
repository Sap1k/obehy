"""`obehy rt replay`: archived payloads through the live pipeline with a simulated clock.

Polls of the selected channels are merged in reception order, decoded by their connector and run
through the same `Runner` as the worker. Housekeeping ticks fall on fixed multiples of the emit
interval, so output never depends on when the replay runs. Outputs:

- `report.json`: decode counts, results by reason, and per DÚK fleet (per source otherwise) the
  share of running key groups (vehicle, trip key, local day) that bound to a journey; with the
  rail feed also names that could not be placed (SŽ points, board rows) and the shadow error of
  each rail predictor (`evaluate.py`);
- `gtfs-rt/<feed>/<UTC time>.pb` every `gtfs_rt_every_s` simulated seconds, if asked;
- with `write_history`, `rt.observation` and history rows for the replayed days (rebuilt).
"""

from __future__ import annotations

import heapq
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from pathlib import Path
from typing import Any, cast

import psycopg

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.archive import ArchivedPoll, iter_polls, succeeded
from obehy.realtime.core import CORE_VERSION, estimate_all
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.gtfs_rt import feed_message
from obehy.realtime.emit.sql_store import SqlStore, clear_observations
from obehy.realtime.evaluate import SAMPLE_EVERY, PredictorLog
from obehy.realtime.facts import Feed, Observation, SourceState, TripKey, VehicleKey
from obehy.realtime.index_sql import ReleaseLoads, ensure_release, file_loads
from obehy.realtime.keyless_report import KeylessLog
from obehy.realtime.manifest import Channel, semantics_by_channel
from obehy.realtime.model import Derivation, Effect, ObservationResult, Unresolved
from obehy.realtime.policy import Policy
from obehy.realtime.quality import QualityLog
from obehy.realtime.runner import Runner, Ticks
from obehy.realtime.sources import duk, mpvnet, pid
from obehy.realtime.sources.registry import DECODERS
from obehy.realtime.times import Instant, instant, local_date, next_multiple

REPORT_SCHEMA_VERSION = 2
LOOKAHEAD_POLLS = 40
SOURCES = ("duk", "sz-mapa", "sz-tabule")


class ReplayError(RuntimeError):
    """The replay cannot run (no decoder for a channel, release not loadable)."""


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    release: str | Path
    archive: Path
    start: date
    end: date
    # The manifests' channels: what to decode, and how the core reads them (semantics).
    channels: Sequence[Channel]
    out: Path
    feeds: tuple[Feed, ...] = ("jdf",)
    write_history: bool = False
    gtfs_rt_every_s: int | None = None
    since: Instant | None = None
    until: Instant | None = None
    # Estimate every tick and log prediction accuracy, flicker and dwell (`quality`).
    quality: bool = False
    # Process every feed the channels decode, also those not live yet (`live_feeds`): to check
    # a connector on recorded days before it goes live. Off: exactly what the worker processes.
    all_feeds: bool = False


@dataclass(slots=True)
class _Stats:
    polls: int = 0
    failed_polls: int = 0
    bad_payloads: int = 0
    observations: int = 0


def _stream(
    archive: Path, source: str, channel: str, start: date, end: date
) -> Iterator[tuple[datetime, str, str, ArchivedPoll]]:
    for poll in iter_polls(archive, source, channel, start, end):
        yield poll.received_at, source, channel, poll


def merged_polls(
    archive: Path, channels: Sequence[tuple[str, str]], start: date, end: date
) -> Iterator[tuple[datetime, str, str, ArchivedPoll]]:
    """The archived polls of `channels` on the UTC days `start`..`end`, in reception order."""

    # One generator function per channel: a generator expression here would read the loop's
    # last (source, channel) for every stream.
    streams = [
        _stream(archive, source, channel, start, end) for source, channel in sorted(channels)
    ]
    return heapq.merge(*streams, key=lambda item: (item[0], item[1], item[2]))


def named_channels(pairs: Sequence[tuple[str, str]]) -> list[Channel]:
    """The shipped manifests' channels named by `(source, channel)` (a pinned corpus's)."""

    from obehy.realtime.record import load_channels

    wanted = set(pairs)
    found = [c for c in load_channels() if (c.source, c.channel) in wanted]
    missing = wanted - {(c.source, c.channel) for c in found}
    if missing:
        raise ReplayError(f"no shipped channel {sorted(missing)[0]}")
    return found


def _polls(options: ReplayOptions) -> Iterator[tuple[datetime, str, str, ArchivedPoll]]:
    keys = [(c.source, c.channel) for c in options.channels]
    return merged_polls(options.archive, keys, options.start, options.end)


def replay(
    connection: psycopg.Connection | None,
    options: ReplayOptions,
    policy: Policy,
    *,
    runner: Runner | None = None,
    report: Callable[[str], None] = print,
) -> dict[str, Any]:
    for channel in options.channels:
        if (channel.source, channel.channel) not in DECODERS:
            raise ReplayError(f"no realtime connector decodes {channel.name} yet")
    writer: Writer | None = None
    if connection is None:
        # No database: the index is read from the release directory's serving files.
        if options.write_history:
            raise ReplayError("writing history needs a database")
        loads = file_loads(Path(options.release))
    else:
        loads = ensure_release(connection, options.release, report=report)
        if options.write_history:
            writer = _writer(connection, loads, options, policy)
    if runner is None:
        runner = Runner(
            connection,
            loads,
            policy,
            options.feeds,
            writer,
            semantics=semantics_by_channel(options.channels),
        )
    else:
        runner.writer = writer
    stats: dict[str, _Stats] = defaultdict(_Stats)
    groups: dict[tuple[str, str, Any, date], bool] = {}
    reasons: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    keyless = KeylessLog(policy.keyless.episode_gap_s)
    predictors = PredictorLog() if "czptt" in runner.runtimes else None
    quality = QualityLog() if options.quality else None
    clock = Clock(runner, options, predictors, quality)

    def stepped(_: Instant, effects: list[Effect]) -> None:
        _count(effects, groups, reasons, unresolved)
        keyless.observe(effects)

    last = drive(runner, _decoded(options, policy, stats), clock, stepped)
    if last is not None:
        finish(runner, last, writer)
    document = _report(loads, options, stats, groups, reasons, runner.skipped)
    if predictors is not None:
        document["unresolved"] = dict(sorted(unresolved.items()))
        document["predictors"] = predictors.report()
    if quality is not None:
        document["quality"] = quality.report()
    options.out.mkdir(parents=True, exist_ok=True)
    if keyless.groups:
        document["keyless"] = keyless.report()
        with atomic_output_path(options.out / "keyless.jsonl") as temporary:
            keyless.write(temporary)
    with atomic_output_path(options.out / "report.json") as temporary:
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    return document


def drive(
    runner: Runner,
    polls: Iterable[tuple[Instant, list[Observation] | None]],
    clock: Clock,
    stepped: Callable[[Instant, list[Effect]], None],
) -> Instant | None:
    """Run the decoded polls through the runner on the simulated clock; `stepped` sees each
    poll's effects. The last poll's time, if any ran."""

    last: Instant | None = None
    for chunk in batched(polls, LOOKAHEAD_POLLS, strict=False):
        # Load the static data of the next polls' keys in one round trip set.
        runner.prefetch([o for _, observations in chunk for o in observations or ()])
        for at, observations in chunk:
            clock.advance(at)
            if observations is None:
                continue
            stepped(at, runner.process(observations))
            last = at
    return last


class Clock:
    """The simulated clock: the runner's ticks (`Ticks`), and with them GTFS-RT snapshots
    every `gtfs_rt_every_s` and the samplers, on round multiples. `snapshot` takes the
    snapshots (default: files under `out`); `ticked` sees each tick's effects."""

    def __init__(
        self,
        runner: Runner,
        options: ReplayOptions,
        predictors: PredictorLog | None = None,
        quality: QualityLog | None = None,
        *,
        snapshot: Callable[[Instant], None] | None = None,
        ticked: Callable[[Instant, list[Effect]], None] | None = None,
    ) -> None:
        self.runner = runner
        self.options = options
        self.predictors = predictors
        self.quality = quality
        self.snapshot: Callable[[Instant], None] = snapshot or self._files
        self.ticks = Ticks(runner, on_tick=self._ticked, on_effects=ticked)
        self.next_snapshot: Instant | None = None
        self.next_sample: Instant | None = None

    def advance(self, at: Instant) -> None:
        every = self.options.gtfs_rt_every_s
        if self.ticks.next is None:
            if every:
                self.next_snapshot = next_multiple(at, every)
            if self.predictors is not None:
                self.next_sample = next_multiple(at, int(SAMPLE_EVERY.total_seconds()))
        self.ticks.advance(at)

    def _files(self, at: Instant) -> None:
        _snapshot(self.options, self.runner, at)

    def _ticked(self, at: Instant) -> None:
        if self.quality is not None:
            for _, runtime in sorted(self.runner.runtimes.items()):
                self.quality.sample(runtime.state, runtime.ctx, at)
        if self.next_sample is not None and self.next_sample <= at:
            rail = self.runner.runtimes["czptt"]
            assert self.predictors is not None
            self.predictors.sample(rail.state, rail.ctx.index, self.next_sample)
            self.next_sample = instant(self.next_sample + SAMPLE_EVERY)
        if self.next_snapshot is not None and self.next_snapshot <= at:
            every = self.options.gtfs_rt_every_s or 0
            self.snapshot(self.next_snapshot)
            self.next_snapshot = instant(self.next_snapshot + timedelta(seconds=every))


def finish(runner: Runner, last: Instant, writer: Writer | None) -> list[Effect]:
    """Final housekeeping and estimates, and the current state when writing history; the
    final tick's effects."""

    effects = runner.tick(last, emit=False)
    if writer is not None:
        writer.write_state({feed: r.state for feed, r in runner.runtimes.items()})
    return effects


@dataclass(frozen=True, slots=True)
class DecodedPoll:
    """One archived poll of the window and its observations; None for a failed poll or, with
    the decoder's `error`, an undecodable payload."""

    at: Instant
    source: str
    channel: str
    poll: ArchivedPoll
    observations: list[Observation] | None
    error: ValueError | None = None


def decoded_polls(options: ReplayOptions, policy: Policy) -> Iterator[DecodedPoll]:
    """Each poll of the window (`since`..`until`) in reception order, decoded; only the
    observations of live feeds unless `all_feeds`."""

    channels = {(c.source, c.channel): c for c in options.channels}
    for received, source, channel, poll in _polls(options):
        at = instant(received)
        if options.until is not None and at >= options.until:
            return
        if options.since is not None and at < options.since:
            continue
        body = poll.body()
        if body is None or not succeeded(poll.entry.get("status"), poll.entry.get("error")):
            yield DecodedPoll(at, source, channel, poll, None)
            continue
        try:
            request = poll.entry.get("request")
            observations = DECODERS[(source, channel)](
                body,
                str(poll.entry["sha256"]),
                at,
                policy.time.max_clock_skew,
                cast(dict[str, Any], request) if isinstance(request, dict) else {},
            )
        except ValueError as error:
            yield DecodedPoll(at, source, channel, poll, None, error)
            continue
        if not options.all_feeds:
            observations = channels[(source, channel)].live_only(observations)
        yield DecodedPoll(at, source, channel, poll, observations)


def _decoded(
    options: ReplayOptions, policy: Policy, stats: dict[str, _Stats]
) -> Iterator[tuple[Instant, list[Observation] | None]]:
    """Each poll in the window, decoded and counted; None for a failed or undecodable poll."""

    for decoded in decoded_polls(options, policy):
        counts = stats[f"{decoded.source}/{decoded.channel}"]
        counts.polls += 1
        if decoded.observations is None:
            if decoded.error is None:
                counts.failed_polls += 1
            else:
                counts.bad_payloads += 1
        else:
            counts.observations += len(decoded.observations)
        yield decoded.at, decoded.observations


def _writer(
    connection: psycopg.Connection, loads: ReleaseLoads, options: ReplayOptions, policy: Policy
) -> Writer:
    return history_writer(
        connection,
        loads,
        policy,
        options.start,
        options.end,
        sorted({channel.source for channel in options.channels}),
    )


def history_writer(
    connection: psycopg.Connection,
    loads: ReleaseLoads,
    policy: Policy,
    start: date,
    end: date,
    sources: Sequence[str],
) -> Writer:
    """A writer for rebuilding the UTC days `start`..`end`: their observations of `sources`
    and their service dates' history are cleared first."""

    writer = Writer(SqlStore(connection), Derivation(CORE_VERSION, policy.version, loads.run_id))
    first = datetime(start.year, start.month, start.day, tzinfo=UTC)
    last = datetime(end.year, end.month, end.day, tzinfo=UTC)
    clear_observations(connection, first, last + timedelta(days=1), sources)
    writer.clear_history([start + timedelta(days=n) for n in range((end - start).days + 1)])
    return writer


def _snapshot(options: ReplayOptions, runner: Runner, at: Instant) -> None:
    for feed, runtime in sorted(runner.runtimes.items()):
        estimate_all(runtime.state, runtime.ctx, at)
        path = options.out / "gtfs-rt" / feed / f"{at:%Y%m%dT%H%M%SZ}.pb"
        path.parent.mkdir(parents=True, exist_ok=True)
        message = feed_message(runtime.state, at, runner.policy.lifecycle)
        with atomic_output_path(path) as temporary:
            temporary.write_bytes(message.SerializeToString(deterministic=True))


def _count(
    effects: Sequence[Effect],
    groups: dict[tuple[str, str, Any, date], bool],
    reasons: Counter[str],
    unresolved: Counter[str],
) -> None:
    for effect in effects:
        if isinstance(effect, Unresolved):
            unresolved[f"{effect.observation.source}/{effect.kind}"] += 1
            continue
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
        if source == duk.SOURCE:
            name = f"{source}/{duk.fleet(vehicle, feed)}"
        elif source == mpvnet.SOURCE:
            name = f"{source}/{'train' if feed == 'czptt' else 'bus'}"
        elif source == pid.SOURCE:
            name = f"{source}/{pid.mode(vehicle, feed)}"
        else:
            name = source
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
