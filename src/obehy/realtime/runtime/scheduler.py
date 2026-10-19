"""Generic poll scheduler: runs a channel at its declared interval, with backoff.

Ticks are fixed-rate; missed ticks are skipped rather than bunched after a slow poll. After
`backoff_after` consecutive failures the interval doubles per failure up to `max_backoff_s`. A
403 means the source refuses us (SŽ blocks addresses): the circuit opens at once and the next
poll waits the full `max_backoff_s`.
No fetch error ends a channel: one that raises is logged and is a failed poll
(`fetch_guarded`). An error handling a poll (the database) still ends the worker.
The recorder and the realtime worker share it and differ only in what they do with a poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import sys
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel

FetchFn = Callable[[Channel], Poll]
BLOCKED = 403
# Enough doublings to reach `max_backoff_s` from any interval the manifests use.
BLOCKED_STEPS = 32
OnPoll = Callable[[Channel, Poll], Awaitable[None]]


async def fetch_guarded(fetcher: Callable[..., Poll], channel: Channel, *args: object) -> Poll:
    """`fetcher(channel, *args)` in a thread; any error it raises is a failed poll."""

    requested_at = datetime.now(UTC)
    try:
        return await asyncio.to_thread(fetcher, channel, *args)
    except Exception as error:
        print(f"{channel.name}: fetch failed", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return Poll(
            requested_at=requested_at,
            received_at=datetime.now(UTC),
            status=None,
            body=None,
            error=f"{type(error).__name__}: {error}",
        )


def backoff_interval(channel: Channel, consecutive_failures: int) -> float:
    if consecutive_failures < channel.backoff_after:
        return channel.interval_s
    doubled = channel.interval_s * 2 ** (consecutive_failures - channel.backoff_after + 1)
    return min(channel.max_backoff_s, max(channel.interval_s, doubled))


def failures_after(channel: Channel, failures: int, poll: Poll) -> int:
    """The consecutive-failure count after `poll`; a 403 opens the circuit at once."""

    if poll.ok:
        return 0
    if poll.status == BLOCKED:
        return max(failures + 1, channel.backoff_after + BLOCKED_STEPS)
    return failures + 1


async def run_channel(
    channel: Channel,
    stop: asyncio.Event,
    fetcher: FetchFn,
    on_poll: OnPoll,
    *,
    once: bool = False,
) -> None:
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    failures = 0
    while not stop.is_set():
        poll = await fetch_guarded(fetcher, channel)
        await on_poll(channel, poll)
        failures = failures_after(channel, failures, poll)
        if once:
            return
        interval = backoff_interval(channel, failures)
        now = loop.time()
        if interval != channel.interval_s:
            next_tick = now + interval
        else:
            next_tick += interval
            if next_tick <= now:
                next_tick += math.ceil((now - next_tick) / interval + 1e-9) * interval
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, next_tick - now))
