"""Warm replay without a database: the replay clock and the reader's chunks."""

from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

from obehy.realtime.core import Context, step
from obehy.realtime.emit import stored
from obehy.realtime.emit.stored import stream_observations, whole_instants
from obehy.realtime.model import FeedState, Observation, ObservationResult, VehicleKey
from obehy.realtime.phases import Phases
from obehy.realtime.policy import load_policy
from obehy.realtime.runner import Runner, shipped_semantics
from obehy.realtime.times import Instant, instant, next_multiple
from obehy.realtime.warm import warm_start
from tests.realtime.observations import observe
from tests.realtime.traces import TRACES, load

POLICY = load_policy()


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


# --- replay by vehicle -------------------------------------------------------------------------


def _selected(
    observations: list[Observation], results: list[ObservationResult]
) -> list[Observation]:
    """What the warm query reads (`emit/stored.py`, mode "vehicles") of a restart after the
    last observation: the rows of live vehicles from their state_since and of their journeys."""

    latest: dict[tuple[str, str], ObservationResult] = {}
    for result in results:
        key = result.observation.first(VehicleKey)
        if key is not None:
            latest[(result.observation.source, key.source_vehicle_id)] = result
    journeys = {r.journey for r in latest.values() if r.journey is not None}
    selected: list[Observation] = []
    for observation, result in zip(observations, results, strict=True):
        key = observation.first(VehicleKey)
        live = None if key is None else latest.get((observation.source, key.source_vehicle_id))
        since = None if live is None else live.state_since
        if (since is not None and observation.received_at >= since) or (result.journey in journeys):
            selected.append(observation)
    return selected


def _run(
    name: str, observations: Sequence[Observation]
) -> tuple[FeedState, list[ObservationResult]]:
    index, _ = load(name)
    ctx = Context(index, POLICY, semantics=shipped_semantics())
    state = FeedState(index.feed)
    results: list[ObservationResult] = []
    for observation in observations:
        effects = step(state, observation, ctx)
        results += [e for e in effects if isinstance(e, ObservationResult)]
    return state, results


@pytest.mark.parametrize("name", [p.stem for p in sorted(TRACES.glob("*.json"))])
def test_replaying_the_live_vehicles_rebuilds_their_state(name: str) -> None:
    _, observations = load(name)
    for cut in range(len(observations) // 4, len(observations) + 1, len(observations) // 4):
        stepped, results = _run(name, observations[:cut])
        # Duplicate fixes are not stored: replay only what has a result.
        stored = [r.observation for r in results]
        replayed, _ = _run(name, _selected(stored, results))
        live = {v for v, s in stepped.vehicles.items() if s.binding is not None}
        for vehicle in live:
            assert replayed.vehicles[vehicle] == stepped.vehicles[vehicle]
            journey = stepped.vehicles[vehicle].binding
            assert journey is not None
            assert replayed.instances[journey.journey] == stepped.instances[journey.journey]


def test_a_bus_on_its_next_journey_is_replayed_from_that_journey() -> None:
    # Bus 209 runs 522586:103 in the morning and 522586:111 in the afternoon.
    index, morning = load("duk-522586-103")
    other, afternoon = load("duk-522586-111")
    for name in ("key_entries", "trips", "service_dates", "locations", "shapes"):
        getattr(index, name).update(getattr(other, name))
    index.loaded_dates |= other.loaded_dates
    observations = morning + afternoon
    ctx = Context(index, POLICY, semantics=shipped_semantics())

    def run(stream: Sequence[Observation]) -> tuple[FeedState, list[ObservationResult]]:
        state = FeedState("jdf")
        results: list[ObservationResult] = []
        for observation in stream:
            results += [
                e for e in step(state, observation, ctx) if isinstance(e, ObservationResult)
            ]
        return state, results

    for cut in (len(morning) + 10, len(morning) + len(afternoon) // 2, len(observations)):
        stepped, results = run(observations[:cut])
        selected = _selected([r.observation for r in results], results)
        replayed, _ = run(selected)

        assert len(selected) < len(results) // 2 + 10  # the morning journey is not replayed
        vehicle = next(iter(stepped.vehicles))
        binding = stepped.vehicles[vehicle].binding
        assert binding is not None and binding.journey.key == "522586:111"
        assert replayed.vehicles[vehicle] == stepped.vehicles[vehicle]
        assert replayed.instances[binding.journey] == stepped.instances[binding.journey]
        assert set(replayed.instances) == {binding.journey}
