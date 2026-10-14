"""Warm replay: rebuild the worker's state from stored observations (docs/R1_SLICE.md section 5).

The stored observations run through the same `Runner` as live ones, with no writer, on the
replay clock: housekeeping ticks on round multiples of the emit tick, each before the first
observation at or after it, as in `obehy rt replay`. Vehicles and journeys a live run forgets
during the window are forgotten here at the same time.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator, Iterable, Iterator
from datetime import datetime, timedelta

from obehy.realtime.model import Observation
from obehy.realtime.phases import Phases
from obehy.realtime.policy import Policy
from obehy.realtime.runner import Runner
from obehy.realtime.times import Instant, next_multiple

PROGRESS_EVERY_S = 30.0


def warm_window(policy: Policy, until: Instant) -> tuple[datetime, datetime, datetime | None]:
    """`(since, until, alive)` for `stored_observations`: the last `max_hours`, and with
    `mode = "vehicles"` the reception time from which a vehicle still counts as live (seen
    within `forget_after_s`, as `refresh` keeps it, plus the clock skew a source time may
    have)."""

    since = until - timedelta(hours=policy.warm_replay_max_hours)
    if policy.warm_replay_mode == "window":
        return since, until, None
    alive_s = policy.lifecycle.forget_after_s + policy.time.max_clock_skew_s
    return since, until, until - timedelta(seconds=alive_s)


class _Ticks:
    """The replay clock's housekeeping ticks."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.every_s = runner.policy.emit_tick_s
        self.next: Instant | None = None

    def due(self, at: Instant) -> bool:
        return self.next is not None and self.next <= at

    def advance(self, at: Instant) -> None:
        """Run every tick up to and including `at`."""

        if self.next is None:
            self.next = next_multiple(at, self.every_s)
        while self.next <= at:
            self.runner.tick(self.next, emit=False)
            self.next = next_multiple(self.next, self.every_s)

    def split(self, chunk: list[Observation]) -> Iterator[list[Observation]]:
        """The chunk in runs between ticks; the ticks between them are run on the way."""

        start = 0
        for i, observation in enumerate(chunk):
            if self.next is None or self.due(observation.received_at):
                if i > start:
                    yield chunk[start:i]
                start = i
                self.advance(observation.received_at)
        if start < len(chunk):
            yield chunk[start:]


def warm_start(
    runner: Runner,
    chunks: Iterable[list[Observation]],
    until: Instant,
    stop: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """Run `chunks` (stored observations in `received_at` order) through the runner without
    side effects, with ticks up to `until`; returns how many. Stops early when `stop` is set."""

    writer, runner.writer = runner.writer, None
    ticks = _Ticks(runner)
    phases = runner.phases  # read here, index and core in the runner: one line
    count = 0
    started = last = time.monotonic()
    reached: Instant | None = None
    iterator = iter(chunks)
    try:
        while stop is None or not stop.is_set():
            with phases.time("read"):
                chunk = next(iterator, None)
            if chunk is None:
                if count:
                    ticks.advance(until)
                break
            for batch in ticks.split(chunk):
                runner.process(batch)
            count += len(chunk)
            reached = chunk[-1].received_at
            now = time.monotonic()
            if log is not None and now - last >= PROGRESS_EVERY_S:
                log(_progress(count, now - started, reached, phases))
                last = now
        if log is not None and count:
            log(_progress(count, time.monotonic() - started, reached, phases))
        return count
    finally:
        runner.writer = writer
        if isinstance(iterator, Generator):
            iterator.close()  # a reader thread stops with it


def _progress(count: int, elapsed_s: float, reached: Instant | None, phases: Phases) -> str:
    at = "-" if reached is None else reached.isoformat(timespec="seconds")
    rate = count / elapsed_s if elapsed_s > 0 else 0.0
    return (
        f"warm replay: {count} observations in {elapsed_s:.0f} s ({rate:.0f}/s), at {at};"
        f" {phases.line(elapsed_s)}"
    )
