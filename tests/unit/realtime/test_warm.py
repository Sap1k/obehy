"""Warm replay without a database: the replay clock and the reader's chunks."""

from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

from obehy.realtime.emit import stored
from obehy.realtime.emit.stored import stream_observations, whole_instants
from obehy.realtime.model import Observation
from obehy.realtime.phases import Phases
from obehy.realtime.runner import Runner
from obehy.realtime.times import Instant, instant, next_multiple
from obehy.realtime.warm import warm_start
from tests.realtime.observations import observe


def _at(start: str, seconds: int) -> Observation:
    observation = observe(start)
    return Observation(
        observation.source,
        observation.channel,
        observation.feed,
        instant(observation.received_at + timedelta(seconds=seconds)),
        observation.observed_at,
        observation.raw,
        observation.decoder_version,
        observation.facts,
    )


@dataclass
class _Policy:
    emit_tick_s: int = 10


@dataclass
class _Runner:
    """What warm replay uses of a `Runner`: the log of ticks and batches."""

    policy: _Policy = field(default_factory=_Policy)
    phases: Phases = field(default_factory=Phases)
    writer: object = "writer"
    log: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])

    def process(self, observations: Sequence[Observation]) -> list[object]:
        assert self.writer is None, "warm replay writes nothing"
        self.log.append(("process", [o.received_at for o in observations]))
        return []

    def tick(self, now: Instant, *, emit: bool = True) -> None:
        assert not emit
        self.log.append(("tick", now))


def test_warm_replay_ticks_on_the_replay_clock() -> None:
    # 08:00:03, :07, :12, :25 local; ticks every 10 s on round multiples.
    observations = [_at("2026-10-09 08:00:03", s) for s in (0, 4, 9, 22)]
    runner = _Runner()
    until = instant(observations[-1].received_at + timedelta(seconds=11))

    count = warm_start(cast(Runner, runner), [observations[:2], observations[2:]], until)

    first = next_multiple(observations[0].received_at, 10)
    assert count == 4
    assert runner.log == [
        ("process", [o.received_at for o in observations[:2]]),
        ("tick", first),
        ("process", [observations[2].received_at]),
        ("tick", instant(first + timedelta(seconds=10))),
        ("process", [observations[3].received_at]),
        ("tick", instant(first + timedelta(seconds=20))),
    ]
    assert runner.writer == "writer"  # restored


def test_warm_replay_stops_when_asked() -> None:
    import threading

    stop = threading.Event()
    stop.set()
    runner = _Runner()
    chunks = [[_at("2026-10-09 08:00:03", 0)]]
    assert warm_start(cast(Runner, runner), chunks, chunks[0][0].received_at, stop) == 0


def test_chunks_never_split_an_instant() -> None:
    a, b, c = (_at("2026-10-09 08:00:00", s) for s in (0, 1, 2))
    batches = [[a, b], [b, b], [b, c], [], [c]]

    chunks = list(whole_instants(batches))

    assert chunks == [[a], [b, b, b, b], [c, c]]


def test_a_reader_error_reaches_the_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    first = [_at("2026-10-09 08:00:00", 0)]

    @contextmanager
    def connect(*_: object, **__: object) -> Generator[None]:
        yield None

    def failing(*_: object, **__: object) -> Iterator[list[Observation]]:
        yield first
        raise RuntimeError("connection lost")

    monkeypatch.setattr(stored.psycopg, "connect", connect)
    monkeypatch.setattr(stored, "stored_observations", failing)
    since = datetime.now().astimezone()
    chunks = stream_observations("postgresql://unused", since, since)

    assert next(chunks) == first
    with pytest.raises(RuntimeError, match="connection lost"):
        next(chunks)
