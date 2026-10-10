"""`obehy realtime`: the long-running realtime worker (BASE_PLAN.md section 18.3).

1. Warm replay: the stored observations of the last `warm_replay.hours` run through the core
   with no side effects, rebuilding in-memory state (there is no checkpoint format).
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
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.core import CORE_VERSION
from obehy.realtime.emit.db import Writer, load_observations
from obehy.realtime.index_sql import ReleaseLoads, active_loads
from obehy.realtime.manifest import Channel, semantics_by_channel
from obehy.realtime.model import Derivation, Feed
from obehy.realtime.policy import Policy
from obehy.realtime.record import ChannelStats, archive_poll, fetch
from obehy.realtime.replay import DECODERS
from obehy.realtime.runner import Runner
from obehy.realtime.runtime.demand import FetchBody, StationDemand, run_demand
from obehy.realtime.runtime.fetch import fetch_body
from obehy.realtime.runtime.scheduler import FetchFn, run_channel
from obehy.realtime.times import Instant, instant


def _now() -> Instant:
    return instant(datetime.now(UTC))


def _log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}", flush=True)


def _writer(connection: psycopg.Connection, loads: ReleaseLoads, policy: Policy) -> Writer:
    return Writer(connection, Derivation(CORE_VERSION, policy.version, loads.run_id))


def warm_start(runner: Runner, connection: psycopg.Connection, now: Instant, hours: int) -> int:
    """Rebuild state from stored observations without side effects; returns how many."""

    writer, runner.writer = runner.writer, None
    try:
        observations = load_observations(connection, now - timedelta(hours=hours), now)
        runner.process(observations)
        if observations:
            runner.tick(now, emit=False)
        return len(observations)
    finally:
        runner.writer = writer


@dataclass(frozen=True, slots=True)
class WorkerOptions:
    archive: Path
    gtfs_rt_dir: Path
    feeds: tuple[Feed, ...] = ("jdf",)
    fetcher: FetchFn = fetch
    body_fetcher: FetchBody = fetch_body  # demand channels: a body per read
    once: bool = False  # poll every channel once and stop (tests)


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
        self.archive = ArchiveWriter(options.archive)
        self.stats: dict[str, ChannelStats] = {}
        self.demand: StationDemand | None = None
        if any(channel.demand is not None for channel in channels):
            # Its own connection: due calls are queried off the event loop's thread.
            demand_connection = psycopg.connect(database_url, autocommit=True)
            self.demand = StationDemand(demand_connection, loads.load_ids["czptt"], policy.boards)

    def start(self) -> None:
        warmed = warm_start(self.runner, self.connection, _now(), self.policy.warm_replay_hours)
        self.runner.writer = _writer(self.connection, self.runner.loads, self.policy)
        self.runner.tick(_now())
        release = self.runner.loads.run_id
        _log(f"realtime worker on release {release}; warm replay of {warmed} observations")

    async def on_poll(self, channel: Channel, poll: Poll) -> None:
        stats = self.stats.setdefault(channel.name, ChannelStats())
        sha256 = archive_poll(self.archive, channel, poll, stats)
        decoder = DECODERS.get((channel.source, channel.channel))
        if decoder is None or not poll.ok or poll.body is None or sha256 is None:
            return
        try:
            observations = decoder(
                poll.body,
                sha256,
                instant(poll.received_at),
                self.policy.time.max_clock_skew,
                poll.request,
            )
        except ValueError as error:
            _log(f"{channel.name}: undecodable payload: {error}")
            return
        async with self.lock:
            self.runner.process(observations)

    async def ticks(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(self.policy.emit_tick_s)
            async with self.lock:
                self.runner.tick(_now())

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
                current = active_loads(self.connection)
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
        for name, channel_stats in sorted(self.stats.items()):
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
    worker.start()
    for channel in channels:
        worker.stats[channel.name] = ChannelStats()
    helpers: list[asyncio.Task[None]] = []
    if not options.once:
        helpers = [asyncio.create_task(worker.ticks()), asyncio.create_task(worker.publications())]
    try:
        await asyncio.gather(*(_poller(worker, channel, options, stop) for channel in channels))
    finally:
        for helper in helpers:
            helper.cancel()
        await worker.close()
    return worker.runner
