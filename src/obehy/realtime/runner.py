"""The one pipeline the worker and replay share: decoded observations → core → effects.

Per batch it loads the static data of the batch's keys (lazy index), steps every observation in
`(received_at, source, item)` order, checks the invariants, and hands the effects to the writer.
`tick` is the emit step: housekeeping, per-feed GTFS-RT files and the current-state tables.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import psycopg

from obehy.realtime.core import (
    Context,
    estimate_all,
    rebase,
    refresh,
    resolve,
    step,
    vehicle_of,
)
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.gtfs_rt import FeedEmitter, write_feed
from obehy.realtime.index import KeyRef
from obehy.realtime.index_sql import IndexLoader, ReleaseLoads
from obehy.realtime.infer.keyed import Match, in_window, span
from obehy.realtime.manifest import semantics_by_channel
from obehy.realtime.model import (
    Effect,
    Feed,
    FeedState,
    LineRef,
    Observation,
    PlatformAssignment,
    ServiceDay,
    SourceSemantics,
    TripKey,
)
from obehy.realtime.phases import Phases
from obehy.realtime.policy import Policy
from obehy.realtime.record import load_channels
from obehy.realtime.times import (
    Instant,
    ServiceTime,
    candidate_service_dates,
    instant,
    local_date,
)


class InvariantError(AssertionError):
    """The core broke one of its own guarantees (progress went back, a date was re-derived)."""


@dataclass(slots=True)
class Batch:
    """One feed's observations of a `process` call, in processing order, and the keys, lines
    and service dates whose static data they need."""

    feed: Feed
    observations: list[Observation]
    refs: set[KeyRef] = field(default_factory=set[KeyRef])
    lines: set[KeyRef] = field(default_factory=set[KeyRef])
    days: set[date] = field(default_factory=set[date])


@dataclass(slots=True)
class FeedRuntime:
    loader: IndexLoader
    state: FeedState
    ctx: Context


def shipped_semantics() -> dict[tuple[str, str], SourceSemantics]:
    """The core semantics of the connector manifests shipped with the package."""

    return semantics_by_channel(load_channels())


def _refs(observation: Observation) -> set[KeyRef]:
    return {(key.namespace, key.key) for key in observation.all(TripKey)}


def _lines(observation: Observation) -> set[KeyRef]:
    """Lines whose trips a keyless observation is inferred among."""

    if observation.first(TripKey) is not None:
        return set()
    return {(line.namespace, line.value) for line in observation.all(LineRef)}


def observation_days(observation: Observation) -> set[date]:
    """The service dates binding the observation may look at: around its time, its source's
    service day, and around a board row's scheduled time (a row read before midnight for a
    train after it binds at the scheduled time)."""

    days = set(candidate_service_dates(observation.at))
    platform = observation.first(PlatformAssignment)
    if platform is not None:
        days.update(candidate_service_dates(platform.scheduled))
    day = observation.first(ServiceDay)
    if day is not None:
        days.add(day.day)
    return days


@dataclass(slots=True)
class Runner:
    connection: psycopg.Connection
    loads: ReleaseLoads
    policy: Policy
    feeds: tuple[Feed, ...]
    writer: Writer | None = None
    gtfs_rt_dir: Path | None = None
    semantics: Mapping[tuple[str, str], SourceSemantics] = field(default_factory=shipped_semantics)
    runtimes: dict[Feed, FeedRuntime] = field(default_factory=dict[Feed, FeedRuntime])
    skipped: int = 0
    phases: Phases = field(default_factory=Phases)
    next_compaction: Instant | None = None
    next_sweep: Instant | None = None
    emitters: dict[Feed, FeedEmitter] = field(default_factory=dict[Feed, FeedEmitter])

    def __post_init__(self) -> None:
        for feed in self.feeds:
            loader = IndexLoader(self.connection, self.loads, feed)
            self.runtimes[feed] = FeedRuntime(
                loader,
                FeedState(feed),
                Context(loader.index, self.policy, semantics=self.semantics),
            )

    def prefetch(self, observations: Sequence[Observation]) -> None:
        """Load the static data of the observations' keys ahead of processing them."""

        refs: dict[Feed, set[KeyRef]] = defaultdict(set)
        lines: dict[Feed, set[KeyRef]] = defaultdict(set)
        days: dict[Feed, set[date]] = defaultdict(set)
        for observation in observations:
            if observation.feed not in self.runtimes:
                continue
            refs[observation.feed].update(_refs(observation))
            lines[observation.feed].update(_lines(observation))
            days[observation.feed].update(observation_days(observation))
        for feed in sorted(days):
            self.runtimes[feed].loader.ensure(refs[feed], days[feed], lines[feed])

    def process(self, observations: Sequence[Observation]) -> list[Effect]:
        batches = self.batches(observations)
        self.load(batches)
        return self.run(batches)

    def batches(self, observations: Sequence[Observation]) -> list[Batch]:
        """The observations per feed, in processing order, with the static data they name."""

        by_feed: dict[Feed, list[Observation]] = defaultdict(list)
        for observation in observations:
            if observation.feed in self.runtimes:
                by_feed[observation.feed].append(observation)
            else:
                self.skipped += 1
        batches: list[Batch] = []
        for feed in sorted(by_feed):
            batch = Batch(
                feed,
                sorted(
                    by_feed[feed],
                    key=lambda o: (o.received_at, o.source, o.raw.sha256, o.raw.item),
                ),
            )
            for observation in batch.observations:
                batch.refs.update(_refs(observation))
                batch.lines.update(_lines(observation))
                batch.days.update(observation_days(observation))
            batches.append(batch)
        return batches

    def needs_index(self, batches: Sequence[Batch]) -> bool:
        """Whether `load` would query the database (the worker then runs it off its loop)."""

        return any(self.runtimes[b.feed].loader.needs(b.refs, b.days, b.lines) for b in batches)

    def load(self, batches: Sequence[Batch]) -> None:
        """Load the static data the batches name that is not in the index yet."""

        for batch in batches:
            with self.phases.time("index"):
                self.runtimes[batch.feed].loader.ensure(batch.refs, batch.days, batch.lines)

    def run(self, batches: Sequence[Batch]) -> list[Effect]:
        """Step the loaded batches through the core and write their effects."""

        effects: list[Effect] = []
        for batch in batches:
            runtime = self.runtimes[batch.feed]
            observations = batch.observations
            runtime.loader.touch(batch.refs, batch.lines, observations[-1].received_at)
            with self.phases.time("core"):
                for observation in observations:
                    effects.extend(self._step(runtime, observation))
            self.phases.count("observations", len(observations))
        if self.writer is not None and effects:
            with self.phases.time("write"):
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

        if self.next_compaction is None:
            self.next_compaction = self._compaction_after(now)
        elif now >= self.next_compaction:
            with self.phases.time("compact"):
                self.compact()
            self.next_compaction = self._compaction_after(now)
        if self.next_sweep is None:
            self.next_sweep = instant(now + timedelta(seconds=self.policy.sweep_index_every_s))
        elif now >= self.next_sweep:
            with self.phases.time("sweep"):
                self.sweep(now)
            self.next_sweep = instant(now + timedelta(seconds=self.policy.sweep_index_every_s))
        for feed, runtime in sorted(self.runtimes.items()):
            with self.phases.time("refresh"):
                refresh(runtime.state, now, self.policy)
            if emit:
                with self.phases.time("estimate"):
                    estimate_all(runtime.state, runtime.ctx)
            if emit and self.gtfs_rt_dir is not None:
                with self.phases.time("gtfs_rt"):
                    emitter = self.emitters.setdefault(feed, FeedEmitter())
                    write_feed(
                        self.gtfs_rt_dir / f"{feed}.pb", emitter.serialize(runtime.state, now)
                    )
        if emit and self.writer is not None:
            with self.phases.time("state"):
                self.writer.write_state({feed: r.state for feed, r in self.runtimes.items()})

    def _compaction_after(self, now: Instant) -> Instant:
        day = local_date(now)
        while True:
            at = ServiceTime(day, self.policy.compact_index_at_s).instant()
            if at > now:
                return at
            day += timedelta(days=1)

    def sweep(self, now: Instant) -> None:
        """Drop the static data and plans of keys not named for `keep_index_s` that no live
        journey needs, in place (BASE_PLAN.md section 18.3); they reload when named again.
        Loading is deterministic, so this never changes output."""

        since = instant(now - timedelta(seconds=self.policy.keep_index_s))
        for runtime in self.runtimes.values():
            state = runtime.state
            bindings = [v.binding for v in state.vehicles.values() if v.binding is not None]
            journeys = set(state.instances) | {b.journey for b in bindings}
            trip_ids = {i.trip_id for i in state.instances.values()} | {b.trip_id for b in bindings}
            day = min([local_date(now) - timedelta(days=1), *(j.service_date for j in journeys)])
            runtime.loader.sweep(since, trip_ids, {(j.namespace, j.key) for j in journeys}, day)
            runtime.ctx.plans.sweep(set(runtime.loader.index.trips), day)

    def compact(self) -> None:
        """Drop the static data and plans no live journey needs (BASE_PLAN.md section 18.3).

        The lazy index only ever grows: every key seen during the day keeps its trips, shapes
        and service dates. A fresh index with just the live journeys' data replaces it, and the
        rest reloads when an observation names it again. Loading is deterministic, so this
        never changes output."""

        for feed, runtime in sorted(self.runtimes.items()):
            state = runtime.state
            journeys = set(state.instances) | {
                v.binding.journey for v in state.vehicles.values() if v.binding is not None
            }
            loader = IndexLoader(self.connection, self.loads, feed)
            loader.ensure(
                {(j.namespace, j.key) for j in journeys}, {j.service_date for j in journeys}
            )
            for journey in sorted(journeys):
                resolve(journey, loader.index)  # rebuilds the run trips of rail journeys
            ctx = Context(loader.index, self.policy, semantics=self.semantics)
            self.runtimes[feed] = FeedRuntime(loader, state, ctx)

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
            ctx = Context(loader.index, self.policy, semantics=self.semantics)
            effects.extend(rebase(runtime.state, loader.index))
            self.runtimes[feed] = FeedRuntime(loader, runtime.state, ctx)
        if self.writer is not None and effects:
            self.writer.write(effects)
        return effects
