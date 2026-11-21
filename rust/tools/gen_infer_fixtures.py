"""Writes the Python reference fixtures for the Rust ports of `runs.py`, `infer/keyed.py` and
`infer/keyless/` (docs/RUST_PORT.md, R5).

    uv run python rust/tools/gen_infer_fixtures.py [--release-dir DIR] [--archive DIR]

Every trace is stepped through the Python core exactly as `tests/realtime/corpus.py`'s `run`
steps it (emit ticks, refresh, estimates), with the functions under test wrapped where the core
(and keyless inference) look them up, so each call's inputs and output are recorded as made:

- `keyed.bind`, `keyed.admissible`, `keyed.span`, `keyed.running`, `runs.run_journey`;
- `keyless.bind`, `keyless.continued`, `keyless.confirm`, `candidates`, `episode.advance`,
  `score.place`, `score.position_residual`;
- `runs.journey_spans`, `runs.part_spans`, `runs.snapshots`, and at each tick, per instance,
  `published_parts`, `current_part`, `current_train` and `run_vehicle_id`.

The traces are the corpus journeys (`tests/realtime/corpus/*.json.gz`, read in place by the Rust
test) and one Arriva Express window (the only keyless source), every vehicle of it in one feed
state, written as a trace of the same format to `fixtures/infer/arriva-window.json.gz` with
the index slice its lines need, loaded from a release's serving files.

Records repeated with the same inputs and output within a trace are kept once, in order of
first appearance (the Rust test replays them in that order on one index per trace, so the run
trips `keyed.bind` builds exist before later records name them). Floats are IEEE bits in hex,
instants microseconds since the epoch, dates ISO.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import importlib
import json
import struct
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import obehy.realtime.core as core_module
import obehy.realtime.infer.keyed as keyed_module
import obehy.realtime.infer.keyless as keyless_module
from obehy.realtime import runs
from obehy.realtime.core import Context, estimate_all, refresh, step
from obehy.realtime.facts import (
    Interval,
    LineRef,
    Observation,
    SourceSemantics,
    TripKey,
    VehicleKey,
)
from obehy.realtime.index import CHILD_NAMESPACE, Index, IndexMiss, Trip
from obehy.realtime.index_sql import IndexLoader, file_loads
from obehy.realtime.infer.keyed import Match
from obehy.realtime.infer.keyless import Decision
from obehy.realtime.infer.keyless.score import Placed
from obehy.realtime.model import (
    Binding,
    FeedState,
    JourneyKey,
    JourneySpan,
    KeylessEpisode,
    LinkJourneys,
    PartSpan,
    Reason,
    SnapshotJourney,
    VehicleId,
    VehicleState,
)
from obehy.realtime.model_json import facts_to_json
from obehy.realtime.policy import load_policy
from obehy.realtime.runner import shipped_semantics
from obehy.realtime.times import Instant, candidate_service_dates, local_date, micros
from tests.realtime.corpus import (  # pyright: ignore[reportPrivateUsage]
    CORPUS,
    Window,
    _decoded,
    _utc,
)
from tests.realtime.traces import from_json

# The package re-exports the function `candidates`, which shadows the module of that name.
candidates_module = importlib.import_module("obehy.realtime.infer.keyless.candidates")

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "rust" / "obehy-core" / "tests" / "fixtures" / "infer"
POLICY = load_policy()
TICK = timedelta(seconds=POLICY.emit_tick_s)
AFTER = timedelta(minutes=35)
RELEASE = ROOT.parent / "artifacts" / "fetched" / "20261012T080259Z-8f416ec58c3a"
ARCHIVE = ROOT.parent / "work" / "rt-extra"
PROBE_EVERY = 4
KEYED_OFFSETS = tuple(
    timedelta(minutes=m) for m in (-1200, -360, -120, -40, -5, 5, 40, 120, 360, 1200)
)
KEYLESS_OFFSETS = tuple(timedelta(minutes=m) for m in (-40, -15, 0, 15, 40))
ARRIVA = Window(
    "arriva-express",
    ("buses",),
    True,
    _utc("2026-10-12T07:00"),
    _utc("2026-10-12T10:00"),
    {},
)


# --- encoding --------------------------------------------------------------------------------


def bits(x: float | None) -> str | None:
    return None if x is None else struct.pack(">d", x).hex()


def us(at: Instant | None) -> int | None:
    return None if at is None else micros(at)


def day(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def journey(j: JourneyKey) -> list[Any]:
    return [j.feed, j.namespace, j.key, j.service_date.isoformat()]


def match(m: Match) -> dict[str, Any]:
    return {"j": journey(m.journey), "t": m.trip.trip_id, "s": us(m.start), "e": us(m.end)}


def found(result: Match | Reason) -> dict[str, Any]:
    return {"r": result.value} if isinstance(result, Reason) else {"m": match(result)}


def interval(i: Interval | None) -> list[int] | None:
    return None if i is None else [micros(i.lo), micros(i.hi)]


def episode(e: KeylessEpisode | None) -> dict[str, Any] | None:
    if e is None:
        return None
    return {
        "line": e.line,
        "dest": e.destination,
        "start": us(e.started_at),
        "last": us(e.last_at),
        "next": e.next_stop,
        "deps": [
            {
                "stop": d.stop,
                "left": us(d.left_at),
                "sched": interval(d.scheduled),
                "pos": None
                if d.position is None
                else [bits(d.position.lat), bits(d.position.lon), bits(d.position.bearing)],
            }
            for d in e.departures
        ],
    }


def binding(b: Binding | None) -> dict[str, Any] | None:
    if b is None:
        return None
    return {
        "v": [b.vehicle.source, b.vehicle.source_vehicle_id],
        "j": journey(b.journey),
        "t": b.trip_id,
        "m": b.method,
        "b": us(b.bound_at),
        "c": b.confirmed,
    }


def spans(items: tuple[JourneySpan, ...]) -> list[Any]:
    return [[journey(s.journey), s.first, s.last] for s in items]


def parts(items: tuple[PartSpan, ...]) -> list[Any]:
    return [[p.trip_id, p.first, p.last] for p in items]


def semantics(s: SourceSemantics) -> list[bool]:
    return [
        s.pre_trip_delay_is_elapsed,
        s.pre_trip_after_end_is_stale,
        s.unchanged_entry_is_no_news,
        s.delay_floor_minute,
    ]


def effect(e: object) -> dict[str, Any]:
    if isinstance(e, LinkJourneys):
        return {"link": [journey(e.source), journey(e.target), e.kind]}
    assert isinstance(e, SnapshotJourney)
    return {
        "j": journey(e.journey),
        "at": us(e.at),
        "rel": e.release_id,
        "t": e.trip_id,
        "rn": e.route_name,
        "hs": e.headsign,
        "calls": [
            [
                c.ordinal,
                c.location_id,
                c.visit_n,
                c.passenger_service,
                c.scheduled_arrival,
                c.scheduled_departure,
                c.name,
            ]
            for c in e.calls
        ],
        "rk": e.run_key,
        "rid": e.route_id,
        "mode": e.mode,
        "ag": e.agency_id,
    }


def observation_json(o: Observation) -> dict[str, Any]:
    return {
        "source": o.source,
        "channel": o.channel,
        "feed": o.feed,
        "received_at": o.received_at.isoformat(),
        "observed_at": None if o.observed_at is None else o.observed_at.isoformat(),
        "sha256": o.raw.sha256,
        "item": o.raw.item,
        "decoder_version": o.decoder_version,
        "facts": facts_to_json(o.facts),
    }


# --- recording -------------------------------------------------------------------------------


class Recorder:
    """The records of one trace, deduplicated in order of first appearance."""

    def __init__(self, observations: list[Observation]) -> None:
        self.position = {id(o): n for n, o in enumerate(observations)}
        self.records: list[dict[str, Any] | None] = []
        self.seen: set[str] = set()

    def obs(self, o: Observation) -> int | dict[str, Any]:
        n = self.position.get(id(o))
        return n if n is not None else observation_json(o)

    def add(self, record: dict[str, Any], slot: int | None = None) -> None:
        text = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if text in self.seen:
            return
        self.seen.add(text)
        if slot is None:
            self.records.append(record)
        else:
            self.records[slot] = record

    def slot(self) -> int:
        """A place for a call's record before the calls it makes (`keyed.bind` builds the run
        trips the `runs.run_journey` inside it reads)."""

        self.records.append(None)
        return len(self.records) - 1

    def kept(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r is not None]


RECORDER: Recorder | None = None


def rec(record: dict[str, Any]) -> None:
    assert RECORDER is not None
    RECORDER.add(record)


def patch(
    module: object, name: str, make: Callable[[Callable[..., Any]], Callable[..., Any]]
) -> None:
    setattr(module, name, make(getattr(module, name)))


def install() -> None:
    def keyed_bind(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(key: TripKey, at: Instant, index: Index, policy: Any, d: date | None = None):
            assert RECORDER is not None
            record = {
                "f": "keyed.bind",
                "key": [key.namespace, key.key],
                "at": us(at),
                "day": day(d),
            }
            slot = RECORDER.slot()
            try:
                out = original(key, at, index, policy, d)
            except IndexMiss:
                RECORDER.add({**record, "out": {"miss": True}}, slot)
                raise
            RECORDER.add({**record, "out": found(out)}, slot)
            return out

        return wrapped

    def admissible(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(b: Binding, at: Instant, index: Index, policy: Any) -> bool:
            out = original(b, at, index, policy)
            rec({"f": "keyed.admissible", "b": binding(b), "at": us(at), "out": out})
            return out

        return wrapped

    def span(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(trip: Trip, j: JourneyKey) -> tuple[Instant, Instant]:
            out = original(trip, j)
            rec(
                {
                    "f": "keyed.span",
                    "t": trip.trip_id,
                    "j": journey(j),
                    "out": [us(out[0]), us(out[1])],
                }
            )
            return out

        return wrapped

    def running(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(index: Index, namespace: str, key: str, days: Any):
            days = tuple(days)
            out = list(original(index, namespace, key, days))
            rec(
                {
                    "f": "keyed.running",
                    "ns": namespace,
                    "key": key,
                    "days": [d.isoformat() for d in days],
                    "out": [[d.isoformat(), t.trip_id] for d, t in out],
                }
            )
            return iter(out)

        return wrapped

    def run_journey(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(feed: str, run: Trip, d: date) -> JourneyKey:
            out = original(feed, run, d)
            rec(
                {
                    "f": "runs.run_journey",
                    "feed": feed,
                    "t": run.trip_id,
                    "day": day(d),
                    "out": journey(out),
                }
            )
            return out

        return wrapped

    def journey_spans(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(feed: str, run: Trip, d: date) -> tuple[JourneySpan, ...]:
            out = original(feed, run, d)
            rec(
                {
                    "f": "runs.journey_spans",
                    "feed": feed,
                    "t": run.trip_id,
                    "day": day(d),
                    "out": spans(out),
                }
            )
            return out

        return wrapped

    def part_spans(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(run: Trip) -> tuple[PartSpan, ...]:
            out = original(run)
            rec({"f": "runs.part_spans", "t": run.trip_id, "out": parts(out)})
            return out

        return wrapped

    def snapshots(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(
            j: JourneyKey, s: tuple[JourneySpan, ...], trip: Trip, index: Index, at: Instant
        ):
            out = original(j, s, trip, index, at)
            rec(
                {
                    "f": "runs.snapshots",
                    "j": journey(j),
                    "spans": spans(s),
                    "t": trip.trip_id,
                    "at": us(at),
                    "out": [effect(e) for e in out],
                }
            )
            return out

        return wrapped

    def advance(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(
            previous: KeylessEpisode | None, o: Observation, s: SourceSemantics, policy: Any
        ):
            out = original(previous, o, s, policy)
            assert RECORDER is not None
            rec(
                {
                    "f": "episode.advance",
                    "prev": episode(previous),
                    "o": RECORDER.obs(o),
                    "sem": semantics(s),
                    "out": episode(out),
                }
            )
            return out

        return wrapped

    def keyless_bind(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(
            line: LineRef, e: KeylessEpisode, o: Observation, index: Index, policy: Any, plans: Any
        ) -> Decision:
            assert RECORDER is not None
            try:
                out: Decision = original(line, e, o, index, policy, plans)
            except IndexMiss:
                rec(
                    {
                        "f": "keyless.bind",
                        "line": [line.namespace, line.value],
                        "ep": episode(e),
                        "o": RECORDER.obs(o),
                        "out": {"miss": True},
                    }
                )
                raise
            rec(
                {
                    "f": "keyless.bind",
                    "line": [line.namespace, line.value],
                    "ep": episode(e),
                    "o": RECORDER.obs(o),
                    "out": {
                        "res": found(out.result),
                        "n": out.candidates,
                        "ev": out.evidence,
                        "best": bits(out.best_s),
                        "ru": bits(out.runner_up_s),
                    },
                }
            )
            return out

        return wrapped

    def continued(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(
            previous: VehicleState | None,
            e: KeylessEpisode,
            at: Instant,
            instances: Any,
            index: Index,
            policy: Any,
        ):
            out = original(previous, e, at, instances, index, policy)
            prev = None
            if previous is not None:
                held = previous.binding is not None and previous.binding.journey in instances
                prev = {
                    "b": binding(previous.binding),
                    "e": episode(previous.episode),
                    "held": held,
                }
            rec(
                {
                    "f": "keyless.continued",
                    "prev": prev,
                    "ep": episode(e),
                    "at": us(at),
                    "out": binding(out),
                }
            )
            return out

        return wrapped

    def confirm(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(b: Binding, e: KeylessEpisode, index: Index, policy: Any) -> Binding:
            out = original(b, e, index, policy)
            rec({"f": "keyless.confirm", "b": binding(b), "ep": episode(e), "out": binding(out)})
            return out

        return wrapped

    def candidates(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(line: LineRef, destination: str, at: Instant, index: Index, policy: Any):
            out = original(line, destination, at, index, policy)
            encoded = (
                {"r": out.value} if isinstance(out, Reason) else {"ms": [match(m) for m in out]}
            )
            rec(
                {
                    "f": "keyless.candidates",
                    "line": [line.namespace, line.value],
                    "dest": destination,
                    "at": us(at),
                    "out": encoded,
                }
            )
            return out

        return wrapped

    def place(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(m: Match, e: KeylessEpisode, index: Index, policy: Any) -> Placed | None:
            out = original(m, e, index, policy)
            placed = (
                None if out is None else {"r": [bits(r) for r in out.residuals], "n": out.next_stop}
            )
            rec({"f": "score.place", "m": match(m), "ep": episode(e), "out": placed})
            return out

        return wrapped

    def position_residual(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(
            m: Match, placed: Placed, o: Observation, index: Index, policy: Any, plans: Any
        ) -> float | None:
            out = original(m, placed, o, index, policy, plans)
            assert RECORDER is not None
            rec(
                {
                    "f": "score.position_residual",
                    "m": match(m),
                    "placed": {"r": [bits(r) for r in placed.residuals], "n": placed.next_stop},
                    "o": RECORDER.obs(o),
                    "out": bits(out),
                }
            )
            return out

        return wrapped

    patch(core_module, "bind", keyed_bind)
    patch(core_module, "admissible", admissible)
    patch(core_module, "span", span)
    patch(core_module, "running", running)
    patch(core_module, "advance", advance)
    patch(core_module, "journey_spans", journey_spans)
    patch(core_module, "part_spans", part_spans)
    patch(core_module, "snapshots", snapshots)
    patch(keyed_module, "run_journey", run_journey)
    patch(keyless_module, "bind", keyless_bind)
    patch(keyless_module, "continued", continued)
    patch(keyless_module, "confirm", confirm)
    patch(keyless_module, "candidates", candidates)
    patch(keyless_module, "place", place)
    patch(keyless_module, "position_residual", position_residual)
    patch(keyless_module, "admissible", admissible)
    patch(keyless_module, "span", span)
    patch(candidates_module, "running", running)
    patch(candidates_module, "span", span)


def instances_at_tick(state: FeedState) -> None:
    for instance in state.instances.values():
        rec(
            {
                "f": "runs.instance",
                "j": journey(instance.journey),
                "t": instance.trip_id,
                "n": len(instance.calls),
                "spans": spans(instance.journeys),
                "parts": parts(instance.parts),
                "left": instance.left_index(),
                "out": {
                    "parts": parts(runs.published_parts(instance)),
                    "part": runs.current_part(instance),
                    "train": runs.current_train(instance),
                    "vid": runs.run_vehicle_id(instance),
                },
            }
        )


def probe(state: FeedState, o: Observation, ctx: Context, n: int) -> None:
    """Calls the core would make at other times: every `PROBE_EVERY`th observation is bound
    again by each of its keys at shifted times (and on a given operating date), its vehicle's
    binding checked at shifted times, and a keyless vehicle inferred again from its episode at
    shifted times, whether or not its binding continues. Inputs the corpus alone does not
    reach: every reason, departures as evidence, index misses."""

    if n % PROBE_EVERY:
        return
    vehicle = o.first(VehicleKey)
    current = (
        state.vehicles.get(VehicleId(o.source, vehicle.source_vehicle_id)) if vehicle else None
    )
    for key in o.all(TripKey):
        for offset in KEYED_OFFSETS:
            with contextlib.suppress(IndexMiss):
                core_module.bind(key, o.at + offset, ctx.index, POLICY, None)
        with contextlib.suppress(IndexMiss):
            core_module.bind(key, o.at, ctx.index, POLICY, local_date(o.at))
    if current is not None and current.binding is not None:
        for offset in KEYED_OFFSETS:
            core_module.admissible(current.binding, o.at + offset, ctx.index, POLICY)
    line = o.first(LineRef)
    if current is None or current.episode is None or line is None or o.first(TripKey) is not None:
        return
    for offset in KEYLESS_OFFSETS:
        shifted = replace(
            o,
            received_at=o.received_at + offset,
            observed_at=None if o.observed_at is None else o.observed_at + offset,
        )
        with contextlib.suppress(IndexMiss):
            keyless_module.bind(line, current.episode, shifted, ctx.index, POLICY, ctx.plans)


def run_trace(index: Index, observations: list[Observation]) -> list[dict[str, Any]]:
    """`tests/realtime/corpus.py`'s `run`, recording."""

    global RECORDER
    RECORDER = Recorder(observations)
    ctx = Context(index, POLICY, semantics=shipped_semantics())
    state = FeedState(index.feed)
    ordered = sorted(observations, key=lambda o: (o.received_at, o.source, o.raw.item))
    first = ordered[0].received_at
    now = Instant(first - timedelta(seconds=first.timestamp() % TICK.total_seconds()) + TICK)
    end = ordered[-1].received_at + AFTER
    pending = iter(ordered)
    upcoming = next(pending, None)
    stepped = 0
    while now <= end:
        while upcoming is not None and upcoming.received_at <= now:
            step(state, upcoming, ctx)
            stepped += 1
            probe(state, upcoming, ctx, stepped)
            upcoming = next(pending, None)
        refresh(state, now, ctx)
        estimate_all(state, ctx, now)
        instances_at_tick(state)
        now = Instant(now + TICK)
    records = RECORDER.kept()
    RECORDER = None
    return records


# --- the Arriva window -----------------------------------------------------------------------


def arriva_trace(release: Path, archive: Path) -> dict[str, Any]:
    """Every observation of the window and the index slice of its lines, as a trace."""

    observations = list(_decoded(ARRIVA, archive))
    lines = sorted(
        {
            (line.namespace, line.value)
            for o in observations
            if (line := o.first(LineRef)) is not None
        }
    )
    days = sorted({d for o in observations for d in candidate_service_dates(o.at)})
    loader = IndexLoader(None, file_loads(release), "jdf")  # type: ignore[arg-type]
    loader.ensure([], frozenset(days), lines)
    index = loader.index
    refs = sorted(
        {
            *lines,
            *((CHILD_NAMESPACE[ns], k) for ns, v in lines for k in index.lines.get((ns, v), ())),
        }
    )
    trips = sorted(
        {
            e.public_id
            for r in refs
            for e in index.key_entries.get(r, ())
            if e.public_id in index.trips
        }
    )
    ordered = [index.trips[t] for t in trips]
    places = sorted({p for t in ordered for c in t.calls for p in (c.location_id, c.placed_at)})
    shapes = sorted({t.shape_id for t in ordered if t.shape_id and t.shape_id in index.shapes})
    wanted = set(days)
    return {
        "release_id": index.release_id,
        "feed": index.feed,
        "keys": [
            [list(r), [[e.public_id, str(e.valid_from), str(e.valid_to)] for e in entries]]
            for r in refs
            if (entries := index.key_entries.get(r)) is not None
        ],
        "lines": [[list(line), list(index.lines[line])] for line in lines if line in index.lines],
        "trips": [
            {
                "trip_id": t.trip_id,
                "route_id": t.route_id,
                "route_name": t.route_name,
                "mode": t.mode,
                "service_id": t.service_id,
                "headsign": t.headsign,
                "shape_id": t.shape_id,
                "run_key": t.run_key,
                "run_part": t.run_part,
                "train_number": t.train_number,
                "agency_id": t.agency_id,
                "calls": [
                    [
                        c.sequence,
                        c.location_id,
                        c.visit_n,
                        c.passenger_service,
                        c.arrival,
                        c.departure,
                        bool(c.on_request),
                        c.point_id,
                    ]
                    for c in t.calls
                ],
            }
            for t in ordered
        ],
        "runs": [],
        # Trips named but not loaded (they run on no loaded date): `running` reads their
        # service here instead of the trip (`Index.running_trip`). Not in the corpus format.
        "trip_services": {
            e.public_id: index.trip_services[e.public_id]
            for r in refs
            for e in index.key_entries.get(r, ())
            if e.public_id not in index.trips and e.public_id in index.trip_services
        },
        "location_keys": [],
        "service_dates": {
            t.service_id: sorted(
                str(d) for d in index.service_dates.get(t.service_id, frozenset[date]()) & wanted
            )
            for t in ordered
        },
        "loaded_dates": [str(d) for d in days],
        "locations": [
            [loc.location_id, loc.name, loc.lon, loc.lat]
            for loc in (index.locations[p] for p in places if p in index.locations)
        ],
        "shapes": [
            {
                "shape_id": s.shape_id,
                "points": [list(p) for p in s.points],
                "distances_m": list(s.distances_m),
            }
            for s in (index.shapes[i] for i in shapes)
        ],
        "observations": [observation_json(o) for o in observations],
    }


def write_gz(path: Path, data: object) -> None:
    """Compact JSON, gzip without a timestamp: the same input writes the same bytes."""

    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
        out.write(text.encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, default=RELEASE)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    args = parser.parse_args()
    install()
    OUT.mkdir(parents=True, exist_ok=True)
    window = arriva_trace(args.release_dir, args.archive)
    window_path = OUT / "arriva-window.json.gz"
    write_gz(window_path, window)
    traces: list[dict[str, Any]] = []
    sources = [
        *((f"tests/realtime/corpus/{p.name}", p) for p in sorted(CORPUS.glob("*.json.gz"))),
        ("rust/obehy-core/tests/fixtures/infer/arriva-window.json.gz", window_path),
    ]
    for name, path in sources:
        with gzip.open(path, "rt", encoding="utf-8") as data:
            trace = json.load(data)
        index, observations = from_json(trace)
        index.trip_services.update(trace.get("trip_services", {}))
        records = run_trace(index, observations)
        traces.append({"trace": name, "calls": records})
        print(f"{name}: {len(records)} records")
    write_gz(OUT / "calls.json.gz", {"policy": POLICY.version, "traces": traces})
    counts: dict[str, int] = {}
    for trace in traces:
        for record in trace["calls"]:
            counts[record["f"]] = counts.get(record["f"], 0) + 1
    print(json.dumps(dict(sorted(counts.items())), indent=1))


if __name__ == "__main__":
    main()
