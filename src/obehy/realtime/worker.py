"""`obehy realtime`: the long-running realtime worker (BASE_PLAN.md section 18.3).

1. Warm replay: the stored observations of the last `warm_replay.hours` run through the core
   with no side effects on a thread, rebuilding in-memory state (there is no checkpoint
   format). Channels are polled and archived meanwhile; their observations wait in a backlog
   that runs, with the writer attached, once the replay is done, so a restart leaves no gap.
2. Every channel is polled by the generic scheduler and archived; channels with a connector are
   decoded and their observations go through the shared `Runner` (core, effects, history).
   Demand channels (SŽ station boards) are read per station while trains are due there
   (`runtime/demand.py`), within the boards budget.
3. An emit tick writes per-feed GTFS-RT files and the current-state tables.
4. `NOTIFY obehy_publication` (sent by `obehy release activate`) rebases live journeys onto the
   new release.
"""

from __future__ import annotations

import asyncio
import gc
import signal
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.core import CORE_VERSION
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.stored import stream_observations
from obehy.realtime.index_sql import ReleaseLoads, active_loads
from obehy.realtime.manifest import Channel, semantics_by_channel
from obehy.realtime.model import Derivation, Feed, Observation
from obehy.realtime.phases import Phases
from obehy.realtime.policy import Policy
from obehy.realtime.record import SUMMARY_INTERVAL_S, ChannelStats, archive_poll, fetch
from obehy.realtime.replay import DECODERS
from obehy.realtime.runner import Runner
from obehy.realtime.runtime.demand import FetchBody, StationDemand, run_demand
from obehy.realtime.runtime.fetch import fetch_body
from obehy.realtime.runtime.scheduler import FetchFn, run_channel
from obehy.realtime.times import Instant, instant
from obehy.realtime.warm import warm_start, warm_window


def _now() -> Instant:
    return instant(datetime.now(UTC))


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}", flush=True)


def _writer(connection: psycopg.Connection, loads: ReleaseLoads, policy: Policy) -> Writer:
    return Writer(connection, Derivation(CORE_VERSION, policy.version, loads.run_id))


@dataclass(frozen=True, slots=True)
class WorkerOptions:
    archive: Path
    gtfs_rt_dir: Path
    feeds: tuple[Feed, ...] = ("jdf",)
    fetcher: FetchFn = fetch
    body_fetcher: FetchBody = fetch_body  # demand channels: a body per read
    once: bool = False  # poll every channel once and stop (tests)


class PollHandler:
    """What the worker does with one poll: archive it, decode it, and run its live feeds'
    observations through the runner. `obehy rt bench` drives the same object with archived
    polls."""

    def __init__(self, runner: Runner, archive: ArchiveWriter, policy: Policy) -> None:
        self.runner = runner
        self.archive = archive
        self.policy = policy
        self.stats: dict[str, ChannelStats] = {}

    def handle(self, channel: Channel, poll: Poll) -> None:
        observations = self.decode(channel, poll, self.runner.phases)
        if observations:
            self.runner.process(observations)

    def decode(self, channel: Channel, poll: Poll, phases: Phases) -> list[Observation]:
        """Archive the poll and decode the observations of its live feeds."""

        stats = self.stats.setdefault(channel.name, ChannelStats())
        with phases.time("archive"):
            sha256, body = archive_poll(self.archive, channel, poll, stats)
        decoder = DECODERS.get((channel.source, channel.channel))
        if decoder is None or not poll.ok or body is None or sha256 is None:
            return []
        with phases.time("decode"):
            try:
                observations = decoder(
                    body,
                    sha256,
                    instant(poll.received_at),
                    self.policy.time.max_clock_skew,
                    poll.request,
                )
            except ValueError as error:
                _log(f"{channel.name}: undecodable payload: {error}")
                return []
            return [o for o in observations if channel.is_live(o.feed)]

    def summary(self, over_s: float) -> list[str]:
        """The channels' poll counts and the phases' share of a core since the last summary."""

        lines = [stats.line(name) for name, stats in sorted(self.stats.items())]
        lines.append(self.runner.phases.line(over_s))
        for stats in self.stats.values():
            stats.latencies_ms.clear()
        self.runner.phases.reset()
        return lines


class _Worker:
    """The running worker: one runner, guarded by a lock shared by polls, ticks and rebases."""

    def __init__(
        self,
        database_url: str,
        channels: Sequence[Channel],
        policy: Policy,
        options: WorkerOptions,
        stop: asyncio.Event,
    ) -> None:
        self.database_url = database_url
        self.policy = policy
        self.options = options
        self.stop = stop
        self.lock = asyncio.Lock()
        self.connection = psycopg.connect(database_url, autocommit=True)
        loads = active_loads(self.connection)
        self.runner = Runner(
            self.connection,
            loads,
            policy,
            options.feeds,
            None,
            options.gtfs_rt_dir,
            semantics_by_channel(channels),
        )
        self.handler = PollHandler(self.runner, ArchiveWriter(options.archive), policy)
        # Until warm replay is done, polls are only archived and decoded into the backlog.
        self.ready = asyncio.Event()
        self.backlog: list[Observation] = []
        self.backlog_phases = Phases()  # the runner's belong to the warm replay thread meanwhile
        self.warm_stop = threading.Event()
        self.demand: StationDemand | None = None
        if any(channel.demand is not None for channel in channels):
            # Its own connection: due calls are queried off the event loop's thread.
            demand_connection = psycopg.connect(database_url, autocommit=True)
            self.demand = StationDemand(demand_connection, loads.load_ids["czptt"], policy.boards)

    async def start(self) -> None:
        """Warm replay on a thread while the channels are polled, then the backlog."""

        until = _now()
        chunks = stream_observations(self.database_url, *warm_window(self.policy, until))
        stopping = asyncio.create_task(self._stop_warm_replay())
        try:
            warmed = await asyncio.to_thread(
                warm_start, self.runner, chunks, until, self.warm_stop, _log
            )
        finally:
            stopping.cancel()
        async with self.lock:
            self.runner.writer = _writer(self.connection, self.runner.loads, self.policy)
            backlog, self.backlog = self.backlog, []
            if backlog:
                self.runner.process(backlog)
            self.runner.phases.reset()
            self.runner.tick(_now())
            self.ready.set()
        # What is alive now lives as long as the worker: keep it out of every GC pass.
        gc.collect()
        gc.freeze()
        release = self.runner.loads.run_id
        _log(
            f"realtime worker on release {release}; warm replay of {warmed} observations,"
            f" {len(backlog)} more received meanwhile"
        )

    async def _stop_warm_replay(self) -> None:
        await self.stop.wait()
        self.warm_stop.set()

    async def on_poll(self, channel: Channel, poll: Poll) -> None:
        if not self.ready.is_set():
            # Nothing awaits between the check and the append: `start` takes the backlog whole.
            self.backlog.extend(self.handler.decode(channel, poll, self.backlog_phases))
            return
        async with self.lock:
            observations = self.handler.decode(channel, poll, self.runner.phases)
            if not observations:
                return
            batches = self.runner.batches(observations)
            if self.runner.needs_index(batches):
                # Static data loads wait on the database: off the loop, so the other channels
                # keep polling meanwhile.
                await asyncio.to_thread(self.runner.load, batches)
            self.runner.run(batches)

    async def ticks(self) -> None:
        await self.ready.wait()
        while not self.stop.is_set():
            await asyncio.sleep(self.policy.emit_tick_s)
            async with self.lock:
                self.runner.tick(_now())

    async def summaries(self) -> None:
        await self.ready.wait()
        last = time.monotonic()
        while not self.stop.is_set():
            await asyncio.sleep(SUMMARY_INTERVAL_S)
            now = time.monotonic()
            async with self.lock:
                lines = self.handler.summary(now - last)
            last = now
            for line in lines:
                _log(line)

    async def publications(self) -> None:
        """Rebase live journeys when `obehy release activate` publishes a new release."""

        listener = await asyncio.to_thread(psycopg.connect, self.database_url, autocommit=True)
        listener.execute("LISTEN obehy_publication")
        try:
            while not self.stop.is_set():
                notes = await asyncio.to_thread(
                    lambda: list(listener.notifies(timeout=5.0, stop_after=1))
                )
                if not notes:
                    continue
                await self.ready.wait()
                current = active_loads(listener)  # the main connection may be loading
                if current.run_id == self.runner.loads.run_id:
                    continue
                async with self.lock:
                    self.runner.switch_release(
                        current, _writer(self.connection, current, self.policy)
                    )
                if self.demand is not None:
                    self.demand.switch(current.load_ids["czptt"])
                _log(f"rebased onto release {current.run_id}")
        finally:
            listener.close()

    async def close(self) -> None:
        async with self.lock:
            self.runner.tick(_now())
        for name, channel_stats in sorted(self.handler.stats.items()):
            _log(channel_stats.line(name))
        self.connection.close()
        if self.demand is not None:
            self.demand.connection.close()


async def _poller(
    worker: _Worker, channel: Channel, options: WorkerOptions, stop: asyncio.Event
) -> None:
    if channel.demand is None:
        await run_channel(channel, stop, options.fetcher, worker.on_poll, once=options.once)
        return
    assert worker.demand is not None
    await run_demand(
        channel,
        stop,
        worker.demand.due,
        options.body_fetcher,
        worker.on_poll,
        worker.policy.boards,
        once=options.once,
    )


async def run_worker(
    database_url: str,
    channels: Sequence[Channel],
    policy: Policy,
    options: WorkerOptions,
    stop: asyncio.Event | None = None,
) -> Runner:
    stop = stop or asyncio.Event()
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    worker = _Worker(database_url, channels, policy, options, stop)
    for channel in channels:
        worker.handler.stats[channel.name] = ChannelStats()

    async def start() -> None:
        try:
            await worker.start()
        except BaseException:
            stop.set()  # the pollers end too
            raise

    helpers: list[asyncio.Task[None]] = []
    if not options.once:
        helpers = [
            asyncio.create_task(worker.ticks()),
            asyncio.create_task(worker.summaries()),
            asyncio.create_task(worker.publications()),
        ]
    try:
        await asyncio.gather(
            start(), *(_poller(worker, channel, options, stop) for channel in channels)
        )
    finally:
        for helper in helpers:
            helper.cancel()
        await worker.close()
    return worker.runner
