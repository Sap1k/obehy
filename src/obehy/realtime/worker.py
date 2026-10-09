"""`obehy realtime`: the long-running realtime worker (BASE_PLAN.md section 18.3).

1. Warm replay: the stored observations of the last `warm_replay.hours` run through the core
   with no side effects, rebuilding in-memory state (there is no checkpoint format).
2. Every channel is polled by the generic scheduler and archived; channels with a connector are
   decoded and their observations go through the shared `Runner` (core, effects, history).
3. An emit tick writes per-feed GTFS-RT files and the current-state tables.
4. `NOTIFY obehy_publication` (sent by `obehy release activate`) rebases live journeys onto the
   new release.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.core import CORE_VERSION
from obehy.realtime.emit.db import Writer, load_observations
from obehy.realtime.index_sql import ReleaseLoads, active_loads
from obehy.realtime.manifest import Channel
from obehy.realtime.model import Derivation, Feed
from obehy.realtime.policy import Policy
from obehy.realtime.record import ChannelStats, archive_poll, fetch
from obehy.realtime.replay import DECODERS
from obehy.realtime.runner import Runner
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


async def run_worker(
    database_url: str,
    channels: Sequence[Channel],
    policy: Policy,
    *,
    archive: Path,
    gtfs_rt_dir: Path,
    feeds: tuple[Feed, ...] = ("jdf",),
    fetcher: FetchFn = fetch,
    once: bool = False,
    stop: asyncio.Event | None = None,
) -> Runner:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    connection = psycopg.connect(database_url, autocommit=True)
    loads = active_loads(connection)
    runner = Runner(connection, loads, policy, feeds, None, gtfs_rt_dir)
    warmed = warm_start(runner, connection, _now(), policy.warm_replay_hours)
    runner.writer = _writer(connection, loads, policy)
    runner.tick(_now())
    _log(f"realtime worker on release {loads.run_id}; warm replay of {warmed} observations")

    archive_writer = ArchiveWriter(archive)
    stats = {channel.name: ChannelStats() for channel in channels}
    lock = asyncio.Lock()

    async def on_poll(channel: Channel, poll: Poll) -> None:
        sha256 = archive_poll(archive_writer, channel, poll, stats[channel.name])
        decoder = DECODERS.get((channel.source, channel.channel))
        if decoder is None or not poll.ok or poll.body is None or sha256 is None:
            return
        try:
            observations = decoder(
                poll.body, sha256, instant(poll.received_at), policy.time.max_clock_skew
            )
        except ValueError as error:
            _log(f"{channel.name}: undecodable payload: {error}")
            return
        async with lock:
            runner.process(observations)

    async def ticks() -> None:
        while not stop.is_set():
            await asyncio.sleep(policy.emit_tick_s)
            async with lock:
                runner.tick(_now())

    async def publications() -> None:
        listener = await asyncio.to_thread(psycopg.connect, database_url, autocommit=True)
        listener.execute("LISTEN obehy_publication")
        try:
            while not stop.is_set():
                notes = await asyncio.to_thread(
                    lambda: list(listener.notifies(timeout=5.0, stop_after=1))
                )
                if not notes:
                    continue
                current = active_loads(connection)
                if current.run_id == runner.loads.run_id:
                    continue
                async with lock:
                    runner.switch_release(current, _writer(connection, current, policy))
                _log(f"rebased onto release {current.run_id}")
        finally:
            listener.close()

    helpers: list[asyncio.Task[None]] = []
    if not once:
        helpers = [asyncio.create_task(ticks()), asyncio.create_task(publications())]
    try:
        await asyncio.gather(
            *(run_channel(channel, stop, fetcher, on_poll, once=once) for channel in channels)
        )
    finally:
        for helper in helpers:
            helper.cancel()
        async with lock:
            runner.tick(_now())
        for name, channel_stats in stats.items():
            _log(channel_stats.line(name))
        connection.close()
    return runner
