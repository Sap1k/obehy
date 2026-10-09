"""Hand-made observations for core tests."""

from __future__ import annotations

from datetime import datetime

from obehy.realtime.model import (
    Delay,
    Fact,
    Feed,
    Observation,
    Position,
    RawRef,
    SourceState,
    TripKey,
    VehicleKey,
)
from obehy.realtime.times import PRAGUE, Instant, instant


def local(text: str) -> Instant:
    """`2026-10-09 00:20` read as Europe/Prague wall-clock time."""

    return instant(datetime.fromisoformat(text).replace(tzinfo=PRAGUE))


def observe(
    at: str,
    *,
    vehicle: str | None = "1001",
    key: str | None = "582492:143",
    namespace: str = "cis:line_trip",
    state: str | None = None,
    delay: int | None = None,
    position: tuple[float, float] | None = None,
    source: str = "duk",
    feed: Feed = "jdf",
) -> Observation:
    """An observation at local time `at`; `position` is (lon, lat)."""

    facts: list[Fact] = []
    if vehicle is not None:
        facts.append(VehicleKey(vehicle))
    if key is not None:
        facts.append(TripKey(namespace, key))
    if position is not None:
        facts.append(Position(lat=position[1], lon=position[0]))
    if delay is not None:
        facts.append(Delay(delay, "unknown"))
    if state is not None:
        facts.append(SourceState(state))
    when = local(at)
    return Observation(source, "vehicles", feed, when, when, RawRef("0" * 64, 0), 1, tuple(facts))
