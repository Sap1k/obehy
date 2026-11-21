"""Writes the Python reference fixtures for the Rust ports of `emit/represent.py`,
`emit/gtfs_rt.py`, the GTFS-RT part of `emit/alerts.py`, `emit/vehicles.py` and
`emit/pairing.py` (docs/RUST_PORT.md, R7).

    uv run python rust/tools/gen_publish_fixtures.py [--minutes N] [--only PART]

Into `rust/obehy-core/tests/fixtures/publish/`:

- `<window>.json.gz`, one per real window (PID 2026-10-15 from 07:00 Prague; DÚK, SŽ map and
  Arriva Express 2026-10-06 from 07:00), run through the Python `Runner` exactly as `obehy rt
  compare --level output` runs it (a `Writer` over a `RecordingStore`, so the vehicle book
  starts as on an empty database), with a snapshot every `EVERY_S`:
  - `book`: every outermost call the writer made to its `VehicleBook` (`observe`,
    `worked_by`, `identify`, `rows`, `track`), in order, with its inputs and what it returned
    (`track`: the sightings it read, the inputs as `VehicleBook.track` reads them), and after
    every snapshot's state write a `checkpoint`: the book's identities, pairings and trains;
  - `snapshots`: on every `DUMP_EVERY`-th snapshot, per feed, the whole `FeedState` (the model
    dataclasses as JSON, below), the vehicle names GTFS-RT used (`VehicleBook.vehicle_id`),
    `representing`, each instance's `publication` and the feed's GTFS-RT bytes (base64), the
    checkpoint they follow named by index.
- `scenarios.json.gz`: every `FeedEmitter.serialize` and `representing` call the GTFS-RT unit
  tests make (test_timeline, test_rail_runs, test_rail_fusion, test_cancellation, test_boards),
  with the state, `now`, the vehicle names and the output.
- `book.json`: a scripted `VehicleBook` (registry keys by source and identifier, curated and
  model values, conflicting live attributes, three sources pairing, a train, a registry
  reload), its calls recorded as the windows' are.
- `alerts.json`: `alert_entity` of hand-made alert rows (the unit test's and variants) and
  `_over` of their periods at several times.

The model as JSON: a dataclass is an object of its fields; an instant is integer microseconds
since the epoch, a date ISO text, a fact `model_json.fact_to_json`, a non-finite float the
text of its repr, tuples, lists and deques lists; a `CallState` and an `Interval` are lists of
their fields in order (they are most of the bytes). `FeedState.vehicles` is the list of states in
insertion order, `instances` and `hops` lists of `[key, value]` in insertion order.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import gzip
import json
import math
import sys
import typing
import zlib
from collections import deque
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from obehy.realtime.core import CORE_VERSION, estimate_all
from obehy.realtime.emit import alerts as alerts_module
from obehy.realtime.emit import represent as represent_module
from obehy.realtime.emit.db import Writer
from obehy.realtime.emit.gtfs_rt import FeedEmitter
from obehy.realtime.emit.pairing import Evidence
from obehy.realtime.emit.recording import RecordingStore
from obehy.realtime.emit.represent import publication, representing
from obehy.realtime.emit.vehicles import VehicleBook, VehicleRows
from obehy.realtime.facts import Fact, Interval, Observation, RawRef, VehicleAttribute, VehicleKey
from obehy.realtime.index_sql import file_loads
from obehy.realtime.manifest import semantics_by_channel
from obehy.realtime.model import (
    CallState,
    Derivation,
    FeedState,
    JourneyKey,
    ObservationResult,
    VehicleId,
)
from obehy.realtime.model_json import fact_to_json
from obehy.realtime.policy import load_policy
from obehy.realtime.replay import (
    Clock,
    DecodedPoll,
    ReplayOptions,
    decoded_polls,
    drive,
    named_channels,
)
from obehy.realtime.runner import Runner
from obehy.realtime.runs import run_vehicle_id
from obehy.realtime.times import PRAGUE, Instant, instant, micros
from obehy.reference.precedence import Profile
from obehy.reference.registry import CuratedValue, Entry, Registry, RegistryKey
from obehy.reference.vocabulary import load_vocabulary

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "rust" / "obehy-core" / "tests" / "fixtures" / "publish"
POLICY = load_policy()
EVERY_S = 60
DUMP_EVERY = 5
FACT_TYPES = typing.get_args(Fact)


def local(text: str) -> Instant:
    """`2026-10-09 07:00` read as Europe/Prague wall-clock time."""

    return instant(datetime.fromisoformat(text).replace(tzinfo=PRAGUE))


@dataclasses.dataclass(frozen=True)
class Window:
    name: str
    archive: Path
    release: Path
    day: date
    channels: tuple[tuple[str, str], ...]
    # Keep the vehicles whose CRC-32 of the source vehicle ID is a multiple of this (fixture
    # size): a real subset of the window, every observation of each vehicle kept.
    sample: int = 1


WINDOWS = (
    Window(
        "pid",
        ROOT / "data" / "rt-raw-prod",
        ROOT / "data" / "releases" / "20261015T014009Z-f4453d48b8d2",
        date(2026, 10, 15),
        (("pid", "vehicles"),),
        sample=6,
    ),
    Window(
        "duk-sz-arriva",
        ROOT.parent / "rt-data" / "rt-raw",
        ROOT / "data" / "releases" / "20261014T065758Z-d732b1d05827",
        date(2026, 10, 6),
        (("duk", "vehicles"), ("sz-mapa", "trains"), ("arriva-express", "buses")),
    ),
)


# --- the model as JSON -------------------------------------------------------------------------


def enc(x: Any) -> Any:
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, Enum):
        return x.value
    if isinstance(x, (str, int)):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else repr(x)
    if isinstance(x, datetime):
        return micros(x)
    if isinstance(x, date):
        return x.isoformat()
    if isinstance(x, FACT_TYPES):
        return fact_to_json(x)
    if isinstance(x, (CallState, Interval)):  # the bulk: positional, in field order
        return [enc(getattr(x, f.name)) for f in dataclasses.fields(x)]
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {f.name: enc(getattr(x, f.name)) for f in dataclasses.fields(x)}
    if isinstance(x, (tuple, list, deque)):
        return [enc(item) for item in x]
    raise TypeError(f"cannot encode {type(x)!r}")


def feed_state(state: FeedState) -> dict[str, Any]:
    for vehicle, current in state.vehicles.items():
        assert vehicle == current.vehicle
    return {
        "feed": state.feed,
        "vehicles": [enc(v) for v in state.vehicles.values()],
        "instances": [[enc(k), enc(i)] for k, i in state.instances.items()],
        "dirty": [enc(j) for j in sorted(state.dirty)],
        "hops": [[enc(k), [enc(t) for t in traversals]] for k, traversals in state.hops.items()],
    }


def journey(j: JourneyKey) -> list[Any]:
    return [j.feed, j.namespace, j.key, j.service_date.isoformat()]


def evidence(e: Evidence) -> list[Any]:
    return [micros(e.first_seen), micros(e.last_seen), e.fixes, journey(e.journey), e.confirmed]


def profile(p: Profile) -> dict[str, Any]:
    return {
        "vehicle_id": p.vehicle_id,
        "registered": p.registered,
        "public_label": p.public_label,
        "flags": dict(p.flags),
        "texts": dict(p.texts),
        "datasets": list(p.datasets),
        "sources": list(p.sources),
        "attributes": [dict(a) for a in p.attributes],
        "last_stated": None if p.last_stated is None else micros(p.last_stated),
    }


def attribute_rows(rows: list[tuple[str, str, str, str, Instant]]) -> list[list[Any]]:
    return [[s, v, a, value, micros(at)] for s, v, a, value, at in rows]


def vehicle_rows(rows: VehicleRows) -> dict[str, Any]:
    return {
        "attributes": attribute_rows(rows.attributes),
        "gone": [[list(a), list(b)] for a, b in rows.gone],
        "kept": [[[list(a), list(b)], evidence(e)] for (a, b), e in rows.kept],
        "rewrite": rows.rewrite,
        "profiles": [profile(p) for p in rows.profiles],
    }


# --- the vehicle book's calls ---------------------------------------------------------------


def sightings(states: dict[str, FeedState]) -> list[list[Any]]:
    """What `VehicleBook.track` reads of the states, in the order it reads it."""

    out: list[list[Any]] = []
    for _, state in sorted(states.items()):
        for vehicle, current in sorted(state.vehicles.items()):
            if current.binding is None:
                continue
            instance = state.instances.get(current.binding.journey)
            train = None
            fix = None
            if instance is not None and instance.journeys:
                train = run_vehicle_id(instance)
            elif current.position is not None and current.status == "running":
                at = current.position_at or current.last_seen
                fix = [
                    journey(current.binding.journey),
                    current.position.lon,
                    current.position.lat,
                    micros(at),
                ]
            out.append(
                [vehicle.source, vehicle.source_vehicle_id, micros(current.last_seen), train, fix]
            )
    return out


def observed(results: list[ObservationResult]) -> list[list[Any]]:
    out: list[list[Any]] = []
    for result in results:
        obs = result.observation
        vehicle = obs.first(VehicleKey)
        if vehicle is None:
            continue
        out.append(
            [
                obs.source,
                vehicle.source_vehicle_id,
                micros(obs.received_at),
                [[f.attribute, f.value] for f in obs.all(VehicleAttribute)],
            ]
        )
    return out


class BookLog:
    """Wraps `VehicleBook`'s methods; records the outermost calls."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.depth = 0
        self.saved: dict[str, Any] = {}

    def install(self) -> None:
        def wrap(name: str, record: Callable[..., dict[str, Any] | None]) -> None:
            original = getattr(VehicleBook, name)
            self.saved[name] = original

            def wrapper(book: VehicleBook, *args: Any) -> Any:
                outer = self.depth == 0
                before = record(book, *args) if outer else None
                self.depth += 1
                try:
                    result = original(book, *args)
                finally:
                    self.depth -= 1
                if outer and before is not None:
                    self.entries.append(before | {"out": out_of(name, result)})
                return result

            setattr(VehicleBook, name, wrapper)

        wrap("observe", lambda _b, results: {"op": "observe", "in": observed(results)})
        wrap(
            "worked_by",
            lambda _b, report, day: {"op": "worked_by", "report": list(report), "day": str(day)},
        )
        wrap(
            "identify",
            lambda _b, report, day: {"op": "identify", "report": list(report), "day": str(day)},
        )

        def rows_in(_b: VehicleBook, rows: list[Any]) -> dict[str, Any]:
            given = attribute_rows(rows)
            last = next((e for e in reversed(self.entries) if e["op"] == "observe"), None)
            # The writer passes what `observe` just returned: named, not repeated.
            same = last is not None and last["out"] == given and given
            return {"op": "rows", "in": "observed" if same else given}

        wrap("rows", rows_in)
        wrap("track", lambda _b, states: {"op": "track", "in": sightings(dict(states))})

    def uninstall(self) -> None:
        for name, original in self.saved.items():
            setattr(VehicleBook, name, original)
        self.saved.clear()

    def checkpoint(self, book: VehicleBook) -> int:
        self.entries.append(
            {
                "op": "checkpoint",
                "identity": [
                    [list(r), found[0], found[1]] for r, found in sorted(book.identity.items())
                ],
                "pairs": [
                    [[list(a), list(b)], evidence(e)]
                    for (a, b), e in sorted(book.pairings.pairs.items())
                ],
                "trains": [[list(r), t] for r, t in sorted(book.trains.items())],
                "clock": None if book.clock is None else micros(book.clock),
            }
        )
        return len(self.entries) - 1


def out_of(name: str, result: Any) -> Any:
    if name == "observe":
        return attribute_rows(result)
    if name == "worked_by":
        return result
    if name == "identify":
        return [result[0], result[1]]
    if name == "rows":
        out = vehicle_rows(result)
        del out["attributes"]  # the input, returned as given
        return out
    return None


# --- the windows ------------------------------------------------------------------------------


def snapshot_feed(
    state: FeedState, at: Instant, identity: Callable[[VehicleId], str], payload: bytes
) -> dict[str, Any]:
    return {
        "feed": state.feed,
        "state": feed_state(state),
        "identity": [[str(v), identity(v)] for v in state.vehicles],
        "representing": sorted(str(v) for v in representing(state)),
        "publication": [
            [journey(k), publication(i, at, POLICY.lifecycle)] for k, i in state.instances.items()
        ],
        "gtfs_rt": base64.b64encode(payload).decode("ascii"),
    }


def sampled(window: Window, polls: Iterator[DecodedPoll]) -> Iterator[DecodedPoll]:
    for poll in polls:
        if window.sample == 1 or poll.observations is None:
            yield poll
            continue
        kept = [
            o
            for o in poll.observations
            if (key := o.first(VehicleKey)) is None
            or zlib.crc32(key.source_vehicle_id.encode("utf-8")) % window.sample == 0
        ]
        yield dataclasses.replace(poll, observations=kept)


def run_window(window: Window, minutes: int) -> dict[str, Any]:
    since = local(f"{window.day} 07:00")
    channels = named_channels(list(window.channels))
    options = ReplayOptions(
        release=window.release,
        archive=window.archive,
        start=window.day - timedelta(days=1),
        end=window.day,
        channels=channels,
        out=OUT,
        feeds=("jdf", "czptt"),
        gtfs_rt_every_s=EVERY_S,
        since=since,
        until=Instant(since + timedelta(minutes=minutes)),
    )
    loads = file_loads(window.release)
    store = RecordingStore()
    runner = Runner(None, loads, POLICY, options.feeds, semantics=semantics_by_channel(channels))
    log = BookLog()
    log.install()
    try:
        writer = Writer(store, Derivation(CORE_VERSION, POLICY.version, loads.run_id))
        runner.writer = writer
        emitters = {feed: FeedEmitter(POLICY.lifecycle) for feed in options.feeds}
        snapshots: list[dict[str, Any]] = []
        count = 0

        def snapshot(at: Instant) -> None:
            nonlocal count
            for _, runtime in sorted(runner.runtimes.items()):
                estimate_all(runtime.state, runtime.ctx, at)
            writer.write_state({feed: r.state for feed, r in runner.runtimes.items()})
            store.take()
            checkpoint = log.checkpoint(writer.vehicles)
            count += 1
            dump = count % DUMP_EVERY == 0
            feeds: list[dict[str, Any]] = []
            for feed, runtime in sorted(runner.runtimes.items()):
                payload = emitters[feed].serialize(
                    runtime.state, at, (), writer.vehicles.vehicle_id
                )
                fresh = FeedEmitter(POLICY.lifecycle).serialize(
                    runtime.state, at, (), writer.vehicles.vehicle_id
                )
                assert payload == fresh
                if dump:
                    feeds.append(
                        snapshot_feed(runtime.state, at, writer.vehicles.vehicle_id, payload)
                    )
            if dump:
                snapshots.append({"at": micros(at), "checkpoint": checkpoint, "feeds": feeds})

        clock = Clock(runner, options, snapshot=snapshot, ticked=lambda _at, _e: store.take())
        drive(
            runner,
            sampled(window, decoded_polls(options, POLICY)),
            clock,
            lambda _p, _e: store.take(),
        )
    finally:
        log.uninstall()
    print(
        f"{window.name}: {len(log.entries)} book calls, {len(snapshots)} snapshots dumped,"
        f" {count} taken"
    )
    return {"window": window.name, "book": log.entries, "snapshots": snapshots}


# --- the unit tests' scenarios ----------------------------------------------------------------

SCENARIO_TESTS = {
    "tests.unit.realtime.test_timeline": (
        "test_gtfs_rt_carries_matched_journeys_and_vehicles_only",
        "test_a_finished_trip_keeps_its_trip_update_for_a_while",
        "test_gtfs_rt_bytes_reused_across_ticks_match_a_fresh_serialization",
        "test_a_bus_reported_by_two_sources_is_one_vehicle_in_gtfs_rt",
        "test_pid_and_duk_on_one_trip_are_one_journey_in_gtfs_rt",
    ),
    "tests.unit.realtime.test_rail_runs": (
        "test_gtfs_rt_has_a_trip_update_per_part_without_railway_points",
    ),
    "tests.unit.realtime.test_rail_fusion": None,
    "tests.unit.realtime.test_cancellation": None,
    "tests.unit.realtime.test_boards": None,
}


def scenarios() -> dict[str, Any]:
    import importlib

    sys.path.insert(0, str(ROOT))
    calls: list[dict[str, Any]] = []
    current = {"test": ""}
    serialize = FeedEmitter.serialize
    represent = represent_module.representing

    def recording_serialize(
        self: FeedEmitter,
        state: FeedState,
        now: Instant,
        alerts: Any = (),
        identity: Callable[[VehicleId], str] = str,
    ) -> bytes:
        payload = serialize(self, state, now, alerts, identity)
        calls.append(
            {
                "test": current["test"],
                "op": "serialize",
                "now": micros(now),
                "state": feed_state(state),
                "identity": [[str(v), identity(v)] for v in state.vehicles],
                "alerts": [base64.b64encode(a).decode("ascii") for a in alerts],
                "out": base64.b64encode(payload).decode("ascii"),
            }
        )
        return payload

    def recording_representing(state: FeedState) -> set[VehicleId]:
        chosen = represent(state)
        calls.append(
            {
                "test": current["test"],
                "op": "representing",
                "state": feed_state(state),
                "out": sorted(str(v) for v in chosen),
            }
        )
        return chosen

    FeedEmitter.serialize = recording_serialize  # type: ignore[method-assign]
    patched = []
    try:
        for name, tests in SCENARIO_TESTS.items():
            module = importlib.import_module(name)
            if hasattr(module, "representing"):
                patched.append(module)
                module.representing = recording_representing  # type: ignore[attr-defined]
            names = tests or tuple(
                n for n in dir(module) if n.startswith("test_") and callable(getattr(module, n))
            )
            for test in names:
                current["test"] = f"{name.rsplit('.', 1)[1]}::{test}"
                before = len(calls)
                getattr(module, test)()
                if len(calls) == before:
                    continue
    finally:
        FeedEmitter.serialize = serialize  # type: ignore[method-assign]
        for module in patched:
            module.representing = represent  # type: ignore[attr-defined]
    calls = [c for c in calls if c["test"]]
    print(f"scenarios: {len(calls)} calls from {len({c['test'] for c in calls})} tests")
    return {"calls": calls}


# --- alerts -----------------------------------------------------------------------------------


def alert_rows() -> list[dict[str, Any]]:
    base: dict[str, Any] = {
        "source": "pid",
        "alert_id": "alert-6de45eb6",
        "cause": "ACCIDENT",
        "effect": "NO_SERVICE",
        "severity": "UNKNOWN_SEVERITY",
        "periods": [{"start": "2026-10-21T13:25:00+00:00", "end": None}],
        "header": {"cs": "Chlumčany u Loun - Vrbno nad Lesy"},
        "description": {},
        "url": {"cs": "https://pid.cz/mimoradnost/?uuid=6de45eb6"},
        "cause_detail": {"cs": "Překážka", "en": "Obstacle"},
        "effect_detail": {},
        "targets": [
            {"kind": "route", "id": "jdf:route:100316", "service_date": None},
            {"kind": "stop", "id": "overlay:pid-gtfs:post:1", "service_date": None},
            {"kind": "trip", "id": "jdf:trip:1", "service_date": "2026-10-21"},
        ],
    }
    rows = [base]
    rows.append(
        base
        | {
            "alert_id": "two-periods",
            "cause": "UNKNOWN_CAUSE",
            "effect": "UNKNOWN_EFFECT",
            "severity": "SEVERE",
            "periods": [
                {"start": "2026-10-20T22:00:00+00:00", "end": "2026-10-21T02:30:00.250000+00:00"},
                {"start": None, "end": "2026-10-22T02:30:00+02:00"},
                {"start": "2026-10-23T05:00:00+01:00", "end": None},
            ],
            "header": {"en": "Works", "cs": "Výluka", "de": "Bauarbeiten"},
            "description": {"cs": "Dlouhý\npopis „v uvozovkách“"},
            "url": {},
            "cause_detail": {},
            "effect_detail": {"cs": "Odklon"},
            "targets": [{"kind": "trip", "id": "czptt:trip:9", "service_date": None}],
        }
    )
    rows.append(
        base
        | {
            "source": "duk",
            "alert_id": "x" * 200,
            "cause": "MEDICAL_EMERGENCY",
            "effect": "ACCESSIBILITY_ISSUE",
            "severity": "WARNING",
            "periods": [],
            "header": {},
            "description": {},
            "url": {},
            "cause_detail": {},
            "effect_detail": {},
            "targets": [],
        }
    )
    causes = [
        "OTHER_CAUSE",
        "TECHNICAL_PROBLEM",
        "STRIKE",
        "DEMONSTRATION",
        "HOLIDAY",
        "WEATHER",
        "MAINTENANCE",
        "CONSTRUCTION",
        "POLICE_ACTIVITY",
        "SPECIAL_EVENT",
    ]
    effects = [
        "REDUCED_SERVICE",
        "SIGNIFICANT_DELAYS",
        "DETOUR",
        "ADDITIONAL_SERVICE",
        "MODIFIED_SERVICE",
        "OTHER_EFFECT",
        "STOP_MOVED",
        "NO_EFFECT",
        "NO_SERVICE",
        "UNKNOWN_EFFECT",
    ]
    for n, (cause, effect) in enumerate(zip(causes, effects, strict=True)):
        rows.append(
            base
            | {
                "alert_id": f"enum-{n}",
                "cause": cause,
                "effect": effect,
                "severity": ["INFO", "WARNING", "SEVERE", "UNKNOWN_SEVERITY"][n % 4],
                "header": {"cs": "a" * (n * 20)},
            }
        )
    return rows


def alerts() -> dict[str, Any]:
    rows = alert_rows()
    times = [
        "2026-10-20T00:00:00+00:00",
        "2026-10-21T02:30:00+00:00",
        "2026-10-21T02:30:01+00:00",
        "2026-10-22T00:30:00+00:00",
        "2026-10-22T00:30:01+00:00",
        "2026-11-01T00:00:00+00:00",
    ]
    over = [
        [alerts_module._over(row["periods"], Instant(datetime.fromisoformat(t))) for t in times]  # pyright: ignore[reportPrivateUsage]
        for row in rows
    ]
    return {
        "rows": rows,
        "entities": [base64.b64encode(alerts_module.alert_entity(r)).decode("ascii") for r in rows],
        "times": times,
        "over": over,
    }


# --- a scripted vehicle book ------------------------------------------------------------------


def registry_json(registry: Registry) -> dict[str, Any]:
    def keys(found: dict[tuple[str, str], list[RegistryKey]]) -> list[Any]:
        return [
            [
                list(k),
                [
                    [r.vehicle_id, r.priority, str(r.valid_from), r.valid_to and str(r.valid_to)]
                    for r in v
                ],
            ]
            for k, v in sorted(found.items())
        ]

    return {
        "by_source": keys(registry.by_source),
        "by_identifier": keys(registry.by_identifier),
        "entries": [
            [e.vehicle_id, e.public_label, e.manufacturer, e.model, list(e.datasets)]
            for e in registry.entries.values()
        ],
        "values": [
            [k, [[c.attribute, c.value, c.dataset, c.priority, c.origin] for c in v]]
            for k, v in sorted(registry.values.items())
        ],
    }


def book_scenario() -> dict[str, Any]:
    """Registry keys by source and by identifier, curated and model values, live attributes
    (conflicting, restated after `touch_s`), three sources pairing on one journey, a train, the
    pair parting, a registry reload: the book's calls as the windows record them."""

    from obehy.realtime.facts import Position
    from obehy.realtime.model import Binding, Freshness, Instance, JourneySpan, VehicleState

    since = date(2026, 1, 1)
    registry = Registry(
        by_source={
            ("duk", "807"): [RegistryKey("reg:bus-1", 2, since, None)],
            ("duk", "900"): [
                RegistryKey("reg:old", 1, since, date(2026, 10, 1)),
                RegistryKey("reg:new", 3, date(2026, 10, 2), None),
            ],
        },
        by_identifier={
            ("licence_plate", "4U12345"): [
                RegistryKey("reg:bus-2", 2, since, None),
                RegistryKey("reg:bus-2a", 2, since, None),
            ],
        },
        entries={
            "reg:bus-1": Entry("reg:bus-1", "807", "Irisbus", "Citelis", ("duk-register",)),
            "reg:bus-2": Entry("reg:bus-2", None, None, None, ("own", "duk-register")),
            "reg:never": Entry("reg:never", "1", None, None, ("own",)),
        },
        values={
            "reg:bus-1": [
                CuratedValue("low_floor", "true", "duk-register", 1, "curated"),
                CuratedValue("manufacturer", "Irisbus", "duk-register", 1, "model"),
                CuratedValue("air_conditioning", "false", "own", 0, "curated"),
                CuratedValue("air_conditioning", "true", "duk-register", 1, "curated"),
            ],
        },
    )
    book = VehicleBook(registry, load_vocabulary())
    log = BookLog()
    log.install()
    trip = JourneyKey("jdf", "cis:line_trip", "582492:143", date(2026, 10, 20))
    other = JourneyKey("jdf", "cis:line_trip", "582492:145", date(2026, 10, 20))

    def obs(
        source: str, vehicle: str, at: Instant, *attributes: tuple[str, str]
    ) -> ObservationResult:
        facts: tuple[Any, ...] = (
            VehicleKey(vehicle),
            *(VehicleAttribute(a, v) for a, v in attributes),
        )
        observation = Observation(source, "vehicles", "jdf", at, at, RawRef("0" * 64, 0), 1, facts)
        return ObservationResult(observation, None, None)

    def state(at: Instant, placed: list[tuple[str, str, JourneyKey, float, str]]) -> FeedState:
        feed = FeedState("jdf")
        run = JourneyKey("jdf", "czptt:pa", "PA1", date(2026, 10, 20))
        feed.instances[run] = Instance(
            run,
            "r",
            "t",
            "rail",
            "running",
            (CallState(1, "A", 1), CallState(2, "B", 1)),
            Freshness(at, at),
            journeys=(
                JourneySpan(
                    JourneyKey("jdf", "czptt:train_number", "6402", run.service_date), 0, 1
                ),
            ),
        )
        for source, vehicle, journey_key, lon, status in placed:
            v = VehicleId(source, vehicle)
            feed.vehicles[v] = VehicleState(
                v,
                "jdf",
                status,  # type: ignore[arg-type]
                at,
                binding=Binding(v, journey_key, "jdf:t1", "keyed", at),
                position=Position(50.66, lon),
                position_at=Instant(at - timedelta(seconds=5)),
            )
        return feed

    def tick(minute: int, placed: list[tuple[str, str, JourneyKey, float, str]]) -> None:
        at = local(f"2026-10-20 08:{minute:02d}")
        book.track({"jdf": state(at, placed)})
        for source, vehicle, *_ in placed:
            book.identify((source, vehicle), at.date())
        book.rows([])
        log.checkpoint(book)

    try:
        t0 = local("2026-10-20 07:59")
        rows = book.observe(
            [
                obs("duk", "807", t0, ("low_floor", "false"), ("operator", "DPMÚL")),
                obs("mpvnet", "4U1 2345", t0, ("licence_plate", "4u1-2345"), ("low_floor", "true")),
                obs("dpmd", "301", t0),
                obs("pid", "9", t0, ("operator", "ČSAD"), ("raw:pid:x", "1")),
                obs("duk", "900", t0),
            ]
        )
        book.rows(rows)
        book.worked_by(("duk", "900"), date(2026, 9, 30))
        book.worked_by(("duk", "900"), date(2026, 10, 20))
        book.worked_by(("dpmd", "301"), date(2026, 10, 20))
        log.checkpoint(book)
        step = 0.0005
        both = [
            ("duk", "807", trip, 13.86, "running"),
            ("mpvnet", "4U1 2345", trip, 13.86 + step, "running"),
            ("dpmd", "301", trip, 13.86 + 2 * step, "running"),
            ("dpmd", "302", trip, 13.86, "layover"),
        ]
        run = JourneyKey("jdf", "czptt:pa", "PA1", date(2026, 10, 20))
        on_run = [("duk", "951", run, 13.0, "running"), ("dpmd", "302", run, 13.0, "running")]
        for minute in range(5):
            tick(minute, both + (on_run if minute in (1, 2) else []))
        later = local("2026-10-20 09:10")
        rows = book.observe(
            [
                obs("duk", "807", later, ("low_floor", "false"), ("operator", "DPMÚL")),
                obs(
                    "mpvnet",
                    "4U1 2345",
                    later,
                    ("licence_plate", "4U12345"),
                    ("low_floor", "false"),
                ),
            ]
        )
        book.rows(rows)
        tick(
            11,
            [("duk", "807", trip, 13.86, "running"), ("mpvnet", "4U1 2345", trip, 13.9, "running")],
        )
        tick(12, [("duk", "807", trip, 13.86, "running"), ("dpmd", "301", other, 13.86, "running")])
        book.registry = Registry(entries={"reg:bus-3": Entry("reg:bus-3", None, None, None, ())})
        log.entries.append({"op": "reload", "registry": registry_json(book.registry)})
        book._everything_dirty()  # pyright: ignore[reportPrivateUsage]
        book.rows([])
        log.checkpoint(book)
        tick(59, [("dpmd", "301", other, 13.86, "running")])
    finally:
        log.uninstall()
    return {"registry": registry_json(registry), "book": log.entries}


def write_gz(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as out,
    ):
        out.write(text.encode("utf-8"))
    print(f"{path.name}: {path.stat().st_size} bytes ({len(text)} uncompressed)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--only", choices=["windows", "scenarios", "alerts", "book"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.only in (None, "alerts"):
        (OUT / "alerts.json").write_text(
            json.dumps(alerts(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    if args.only in (None, "book"):
        (OUT / "book.json").write_text(
            json.dumps(book_scenario(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    if args.only in (None, "scenarios"):
        write_gz(OUT / "scenarios.json.gz", scenarios())
    if args.only in (None, "windows"):
        for window in WINDOWS:
            write_gz(OUT / f"{window.name}.json.gz", run_window(window, args.minutes))


if __name__ == "__main__":
    main()
