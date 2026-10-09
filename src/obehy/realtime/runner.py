"""The one pipeline the worker and replay share: decoded observations → core → effects.

Per batch it loads the static data of the batch's keys (lazy index), steps every observation in
`(received_at, source, item)` order, checks the invariants, and hands the effects to the writer.
`tick` is the emit step: housekeeping, per-feed GTFS-RT files and the current-state tables.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg

from obehy.realtime.core import Context, estimate_all, rebase, refresh, step, vehicle_of
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.gtfs_rt import feed_message, write_feed
from obehy.realtime.index import KeyRef
from obehy.realtime.index_sql import IndexLoader, ReleaseLoads
from obehy.realtime.infer.keyed import Match, in_window, span
from obehy.realtime.model import Effect, Feed, FeedState, Observation, TripKey
from obehy.realtime.policy import Policy
from obehy.realtime.times import Instant, candidate_service_dates


class InvariantError(AssertionError):
    """The core broke one of its own guarantees (progress went back, a date was re-derived)."""


@dataclass(slots=True)
class FeedRuntime:
    loader: IndexLoader
    state: FeedState
    ctx: Context


@dataclass(slots=True)
class Runner:
    connection: psycopg.Connection
    loads: ReleaseLoads
    policy: Policy
    feeds: tuple[Feed, ...]
    writer: Writer | None = None
    gtfs_rt_dir: Path | None = None
    runtimes: dict[Feed, FeedRuntime] = field(default_factory=dict[Feed, FeedRuntime])
    skipped: int = 0

    def __post_init__(self) -> None:
        for feed in self.feeds:
            loader = IndexLoader(self.connection, self.loads, feed)
            self.runtimes[feed] = FeedRuntime(
                loader, FeedState(feed), Context(loader.index, self.policy)
            )

    def prefetch(self, observations: Sequence[Observation]) -> None:
        """Load the static data of the observations' keys ahead of processing them."""

        refs: dict[Feed, set[KeyRef]] = defaultdict(set)
        days: dict[Feed, set[date]] = defaultdict(set)
        for observation in observations:
            if observation.feed not in self.runtimes:
                continue
            key = observation.first(TripKey)
            if key is not None:
                refs[observation.feed].add((key.namespace, key.key))
            days[observation.feed].update(candidate_service_dates(observation.at))
        for feed in sorted(days):
            self.runtimes[feed].loader.ensure(refs[feed], days[feed])

    def process(self, observations: Sequence[Observation]) -> list[Effect]:
        by_feed: dict[Feed, list[Observation]] = defaultdict(list)
        for observation in observations:
            if observation.feed in self.runtimes:
                by_feed[observation.feed].append(observation)
            else:
                self.skipped += 1
        effects: list[Effect] = []
        for feed in sorted(by_feed):
            runtime = self.runtimes[feed]
            batch = sorted(
                by_feed[feed], key=lambda o: (o.received_at, o.source, o.raw.sha256, o.raw.item)
            )
            refs: set[KeyRef] = set()
            days: set[date] = set()
            for observation in batch:
                key = observation.first(TripKey)
                if key is not None:
                    refs.add((key.namespace, key.key))
                days.update(candidate_service_dates(observation.at))
            runtime.loader.ensure(refs, days)
            for observation in batch:
                effects.extend(self._step(runtime, observation))
        if self.writer is not None and effects:
            self.writer.write(effects)
        return effects

    def _step(self, runtime: FeedRuntime, observation: Observation) -> list[Effect]:
        state = runtime.state
        vehicle = vehicle_of(observation)
        before = state.vehicles.get(vehicle) if vehicle is not None else None
        old_committed = None
        if before is not None and before.binding is not None:
            instance = state.instances.get(before.binding.journey)
            if instance is not None and instance.track is not None:
                old_committed = instance.track.committed_m
        effects = step(state, observation, runtime.ctx)
        after = state.vehicles.get(vehicle) if vehicle is not None else None
        if before is None or before.binding is None or after is None or after.binding is None:
            return effects
        old, new = before.binding, after.binding
        if old.journey == new.journey:
            # The live position may be revised between hypotheses; committed progress never.
            track = state.instances[new.journey].track
            if (
                old_committed is not None
                and track is not None
                and track.committed_m < old_committed
            ):
                raise InvariantError(f"committed progress of {new.journey} went back")
        elif old.journey.namespace == new.journey.namespace and old.journey.key == new.journey.key:
            trip = runtime.ctx.index.trip(old.trip_id)
            match = Match(old.journey, trip, *span(trip, old.journey))
            if in_window(match, observation.at, self.policy):
                raise InvariantError(f"{old.journey} was re-dated while still admissible")
        return effects

    def tick(self, now: Instant, *, emit: bool = True) -> None:
        """Housekeeping at `now`; with `emit`, also GTFS-RT files and the state tables."""

        for feed, runtime in sorted(self.runtimes.items()):
            refresh(runtime.state, now, self.policy)
            if emit:
                estimate_all(runtime.state, runtime.ctx)
            if emit and self.gtfs_rt_dir is not None:
                write_feed(self.gtfs_rt_dir / f"{feed}.pb", feed_message(runtime.state, now))
        if emit and self.writer is not None:
            self.writer.write_state({feed: r.state for feed, r in self.runtimes.items()})

    def switch_release(self, loads: ReleaseLoads, writer: Writer | None) -> list[Effect]:
        """Rebase live journeys onto a newly activated release (mid-day activation)."""

        self.loads = loads
        self.writer = writer
        effects: list[Effect] = []
        for feed, runtime in sorted(self.runtimes.items()):
            loader = IndexLoader(self.connection, loads, feed)
            journeys = sorted(runtime.state.instances)
            loader.ensure(
                {(j.namespace, j.key) for j in journeys}, {j.service_date for j in journeys}
            )
            ctx = Context(loader.index, self.policy)
            effects.extend(rebase(runtime.state, loader.index))
            self.runtimes[feed] = FeedRuntime(loader, runtime.state, ctx)
        if self.writer is not None and effects:
            self.writer.write(effects)
        return effects
