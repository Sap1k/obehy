"""`obehy rt replay`: archived payloads through the live pipeline with a simulated clock.

Polls of the selected channels are merged in reception order, decoded by their connector and run
through the same `Runner` as the worker. Housekeeping ticks fall on fixed multiples of the emit
interval, so output never depends on when the replay runs. Outputs:

- `report.json` (`replay_report.py`): decode counts, results by reason, the share of running key
  groups that bound, with the rail feed the names that could not be placed and the shadow error
  of each rail predictor, with `quality` the prediction quality. It is read from the run's
  output stream (the diagnostics records of `obehy rt compare --diagnostics`, canonical form),
  not from the core: `obehy rt report --from-stream` makes the same report from a stream file;
- `gtfs-rt/<feed>/<UTC time>.pb` every `gtfs_rt_every_s` simulated seconds, if asked;
- with `write_history`, `rt.observation` and history rows for the replayed days (rebuilt).
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from pathlib import Path
from typing import Any, cast

import psycopg

from obehy.pipeline.files import atomic_output_path
from obehy.realtime.archive import ArchivedPoll, iter_polls, succeeded
from obehy.realtime.canonical import plain
from obehy.realtime.core import CORE_VERSION, estimate_all
from obehy.realtime.emit.db import Writer, observation_row
from obehy.realtime.emit.gtfs_rt import feed_message
from obehy.realtime.emit.recording import observation_record
from obehy.realtime.emit.samples import PREDICTOR_SAMPLE_EVERY_S, JourneySampler, predictor_sample
from obehy.realtime.emit.sql_store import SqlStore, clear_observations
from obehy.realtime.facts import FACT_SCHEMA_VERSION, Feed, Observation
from obehy.realtime.index_sql import ReleaseLoads, ensure_release, file_loads
from obehy.realtime.manifest import Channel, semantics_by_channel
from obehy.realtime.model import Derivation, Effect, ObservationResult, Unresolved
from obehy.realtime.policy import Policy
from obehy.realtime.replay_report import ReplayReport
from obehy.realtime.runner import Runner, Ticks
from obehy.realtime.sources.registry import DECODER_VERSIONS, DECODERS
from obehy.realtime.times import Instant, instant, next_multiple

LOOKAHEAD_POLLS = 40
SOURCES = ("duk", "sz-mapa", "sz-tabule")
COMPARE_VERSION = 1
Record = dict[str, Any]
Emit = Callable[[Record], None]


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
    # Sample every journey every tick for prediction accuracy, flicker and dwell (`quality`).
    quality: bool = False
    # Process every feed the channels decode, also those not live yet (`live_feeds`): to check
    # a connector on recorded days before it goes live. Off: exactly what the worker processes.
    all_feeds: bool = False
    # `obehy rt compare`: add the records the replay report reads (`poll`, `predictor_sample`);
    # `quality` adds `journey_sample` too. Replay itself always reads them.
    diagnostics: bool = False


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


def header(options: ReplayOptions, level: str, policy: Policy, run_id: str | None = None) -> Record:
    """The stream's first record (`compare.py`); `run_id` for the output level."""

    record: Record = {
        "kind": "header",
        "compare_version": COMPARE_VERSION,
        "level": level,
        "channels": sorted(c.name for c in options.channels),
        "from": options.start,
        "to": options.end,
        "since": options.since,
        "until": options.until,
        "all_feeds": options.all_feeds,
        "fact_schema_version": FACT_SCHEMA_VERSION,
    }
    if level == "output":
        record |= {
            "feeds": list(options.feeds),
            "gtfs_rt_every_s": options.gtfs_rt_every_s,
            "release": run_id,
            "core_version": CORE_VERSION,
            "policy_version": policy.version,
        }
        if options.diagnostics or options.quality:
            record["diagnostics"] = True
        if options.quality:
            record["quality"] = True
    return record


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
    head = header(options, "output", policy, loads.run_id) | {"diagnostics": True}
    diagnostics = ReplayReport(plain(head), policy.keyless.episode_gap_s)

    def emit(record: Record) -> None:
        # JSON-ready as a stream file reads back, but not rounded: the report reads no float a
        # stream rounds (`journey_sample` floats are exact), so a file gives the same report.
        diagnostics.add(plain(record))

    def stepped(poll: DecodedPoll, effects: list[Effect] | None) -> None:
        emit(poll_record(poll, output=True))
        for record in step_records(poll.at, effects or (), loads.run_id):
            emit(record)

    sampled, rail_sampled = samplers(runner, options, emit)
    clock = Clock(runner, options, sampled=sampled, rail_sampled=rail_sampled)
    last = drive(runner, decoded_polls(options, policy), clock, stepped)
    if last is not None:
        finish(runner, last, writer)
    return diagnostics.write(options.out)


def step_records(at: Instant, effects: Iterable[Effect], release_id: str) -> Iterator[Record]:
    """Of a step's effects and rows, the records the replay report reads: the `Unresolved`
    effects and the `observation` rows (as the writer makes them)."""

    results: list[ObservationResult] = []
    for effect in effects:
        if isinstance(effect, Unresolved):
            yield {"kind": "effect", "at": at, "phase": "step", "effect": effect}
        elif isinstance(effect, ObservationResult):
            results.append(effect)
    for result in results:
        yield observation_record(observation_row(result, release_id)) | {"at": at}


def poll_record(poll: DecodedPoll, *, output: bool = False) -> Record:
    """A poll's `poll` record; at the output level with its observations per feed."""

    entry = poll.poll.entry
    request = entry.get("request")
    record: Record = {
        "kind": "poll",
        "at": poll.at,
        "source": poll.source,
        "channel": poll.channel,
        "sha256": entry.get("sha256"),
        "status": entry.get("status"),
        "error": entry.get("error"),
        "request": request if isinstance(request, dict) else None,
        "decoder_version": DECODER_VERSIONS[(poll.source, poll.channel)],
    }
    observations = poll.observations
    if observations is None:
        return record | {"outcome": "failed" if poll.error is None else "undecodable"}
    record |= {"outcome": "decoded", "observations": len(observations)}
    if output:
        feeds: dict[str, int] = {}
        for observation in observations:
            feeds[observation.feed] = feeds.get(observation.feed, 0) + 1
        record["feeds"] = feeds
    return record


def samplers(
    runner: Runner, options: ReplayOptions, emit: Emit
) -> tuple[Callable[[Instant], None] | None, Callable[[Instant], None] | None]:
    """The tick's `journey_sample` writer (with `quality`) and the rail feed's
    `predictor_sample` writer (with the rail feed), both writing to `emit`."""

    sampled: Callable[[Instant], None] | None = None
    if options.quality:
        sampler = JourneySampler()

        def journeys(at: Instant) -> None:
            for _, runtime in sorted(runner.runtimes.items()):
                emit(sampler.sample(runtime.state, runtime.ctx, at))

        sampled = journeys
    if "czptt" not in runner.runtimes:
        return sampled, None

    def predictors(at: Instant) -> None:
        rail = runner.runtimes["czptt"]
        emit(predictor_sample(rail.state, rail.ctx.index, at))

    return sampled, predictors


def drive(
    runner: Runner,
    polls: Iterable[DecodedPoll],
    clock: Clock,
    stepped: Callable[[DecodedPoll, list[Effect] | None], None],
) -> Instant | None:
    """Run the decoded polls through the runner on the simulated clock; `stepped` sees each
    poll and its effects (None: failed or undecodable). The last poll's time, if any ran."""

    last: Instant | None = None
    for chunk in batched(polls, LOOKAHEAD_POLLS, strict=False):
        # Load the static data of the next polls' keys in one round trip set.
        runner.prefetch([o for poll in chunk for o in poll.observations or ()])
        for poll in chunk:
            clock.advance(poll.at)
            if poll.observations is None:
                stepped(poll, None)
                continue
            stepped(poll, runner.process(poll.observations))
            last = poll.at
    return last


class Clock:
    """The simulated clock: the runner's ticks (`Ticks`), and with them GTFS-RT snapshots
    every `gtfs_rt_every_s` and the samples, on round multiples. `snapshot` takes the
    snapshots (default: files under `out`); `ticked` sees each tick's effects; after each tick
    `sampled` (the journey samples), then on each predictor sample time `rail_sampled`, then the
    snapshot."""

    def __init__(
        self,
        runner: Runner,
        options: ReplayOptions,
        *,
        snapshot: Callable[[Instant], None] | None = None,
        ticked: Callable[[Instant, list[Effect]], None] | None = None,
        sampled: Callable[[Instant], None] | None = None,
        rail_sampled: Callable[[Instant], None] | None = None,
    ) -> None:
        self.runner = runner
        self.options = options
        self.sampled = sampled
        self.rail_sampled = rail_sampled
        self.snapshot: Callable[[Instant], None] = snapshot or self._files
        self.ticks = Ticks(runner, on_tick=self._ticked, on_effects=ticked)
        self.next_snapshot: Instant | None = None
        self.next_sample: Instant | None = None

    def advance(self, at: Instant) -> None:
        every = self.options.gtfs_rt_every_s
        if self.ticks.next is None:
            if every:
                self.next_snapshot = next_multiple(at, every)
            if self.rail_sampled is not None:
                self.next_sample = next_multiple(at, PREDICTOR_SAMPLE_EVERY_S)
        self.ticks.advance(at)

    def _files(self, at: Instant) -> None:
        _snapshot(self.options, self.runner, at)

    def _ticked(self, at: Instant) -> None:
        if self.sampled is not None:
            self.sampled(at)
        if self.next_sample is not None and self.next_sample <= at:
            assert self.rail_sampled is not None
            self.rail_sampled(self.next_sample)
            self.next_sample = instant(
                self.next_sample + timedelta(seconds=PREDICTOR_SAMPLE_EVERY_S)
            )
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
