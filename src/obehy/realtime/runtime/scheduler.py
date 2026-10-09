"""Generic poll scheduler: runs a channel at its declared interval, with backoff.

Ticks are fixed-rate; missed ticks are skipped rather than bunched after a slow poll. After
`backoff_after` consecutive failures the interval doubles per failure up to `max_backoff_s`.
The recorder and the realtime worker share it and differ only in what they do with a poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable

from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel

FetchFn = Callable[[Channel], Poll]
OnPoll = Callable[[Channel, Poll], Awaitable[None]]


def backoff_interval(channel: Channel, consecutive_failures: int) -> float:
    if consecutive_failures < channel.backoff_after:
        return channel.interval_s
    doubled = channel.interval_s * 2 ** (consecutive_failures - channel.backoff_after + 1)
    return min(channel.max_backoff_s, max(channel.interval_s, doubled))


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
        poll = await asyncio.to_thread(fetcher, channel)
        await on_poll(channel, poll)
        failures = 0 if poll.ok else failures + 1
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
