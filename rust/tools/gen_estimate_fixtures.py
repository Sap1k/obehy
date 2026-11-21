"""Writes the Python reference fixture for the Rust ports of `timeline/__init__.py` (`advance`),
`timeline/fusion.py`, `timeline/estimate.py` and `platforms.py` (docs/RUST_PORT.md, R4).

    uv run python rust/tools/gen_estimate_fixtures.py [--detail-journeys N] [--out PATH]

Nothing is re-implemented here: the real core runs and every call it makes into these modules
is recorded from outside with its inputs and output, at full precision:

- the corpus journeys (`tests/realtime/corpus.py`, about a hundred real journeys of every
  source and mode), stepped through the core with the replay's emit ticks;
- the unit scenarios of `test_timeline.py`, `test_rail_fusion.py`, `test_cancellation.py`,
  `test_boards.py` and `test_hot_path.py`, run under pytest in this process.

Recorded per group (a journey or a test), in call order:

- `adv`: `timeline.advance` (instance, trip, observation, semantics, the vehicle's previous fix
  -> instance and effects); `point`: `fusion.apply_point` inside it; in the unit tests and the
  first `--detail-journeys` journeys also `note` (`note_source`) and `usable`
  (`usable_delay`);
- `lead`: `fusion.with_lead`;
- `est`: one journey of `core.estimate_all`: the instance before, `now`, the predictor, every
  intermediate (`current_lateness`, `waiting_is_not_early` + `dwell_credit`,
  `anchor_prediction`, `running_extra`, `_fresh`, `last_fix_at_stop`, `_seen_leaving`,
  `minimum_drives`) and the instance after `monotone`;
- `plat`: `platforms.apply_platform` (the bind result, the instance `_open` left, effects and
  the instance after).

The policy is the loaded one (asserted; `est` records carry the predictor, which one test
changes). A group refers to its index: a corpus journey's own trace file (`corpus`), or a unit
test's index dumped in the trace format (`indexes`, only what plans and station lookups read).
Trips and observations are written once per group (`trips`, `observations`) and named by
number. An instance is written whole the first time its journey appears in a group and after
that as the fields that changed against the journey's previous one (`"~": 1`; `calls~` lists the
changed calls, null for unchanged ones), in record order (in, then out). Floats are JSON numbers
(repr, exact) with "inf", "-inf", "nan" and "-0.0" as strings; instants integer microseconds.
Output: `estimate.json.gz`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from obehy.realtime import core as core_module  # noqa: E402
from obehy.realtime import timeline as timeline_module  # noqa: E402
from obehy.realtime.facts import Interval, Observation, Position, SourceSemantics  # noqa: E402
from obehy.realtime.index import Index, Trip  # noqa: E402
from obehy.realtime.infer.keyed import Match  # noqa: E402
from obehy.realtime.model import (  # noqa: E402
    CallState,
    Instance,
    Lead,
    ObservationResult,
    Platform,
    RecordPlatform,
    SourceTrack,
    Track,
    Unresolved,
    WriteEvent,
)
from obehy.realtime.model_json import facts_to_json  # noqa: E402
from obehy.realtime.policy import Policy, load_policy  # noqa: E402
from obehy.realtime.times import Instant, micros  # noqa: E402

OUT = ROOT / "rust" / "obehy-core" / "tests" / "fixtures" / "estimate.json.gz"
POLICY = load_policy()
TESTS = (
    "tests/unit/realtime/test_timeline.py",
    "tests/unit/realtime/test_rail_fusion.py",
    "tests/unit/realtime/test_cancellation.py",
    "tests/unit/realtime/test_boards.py",
    "tests/unit/realtime/test_hot_path.py",
)


# --- values -------------------------------------------------------------------------------------


def f(x: float | None) -> float | str | None:
    if x is None:
        return None
    x = float(x)
    if math.isnan(x):
        return "nan"
    if math.isinf(x):
        return "inf" if x > 0 else "-inf"
    if x == 0.0 and math.copysign(1.0, x) < 0:
        return "-0.0"
    return x


def t(at: Instant | None) -> int | None:
    return None if at is None else micros(at)


def td(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds


def iv(value: Interval | None) -> list[Any] | None:
    return None if value is None else [t(value.lo), t(value.hi)]


def journey(j: Any) -> list[Any]:
    return [j.feed, j.namespace, j.key, j.service_date.isoformat()]


def position(p: Position | None) -> list[Any] | None:
    return None if p is None else [f(p.lat), f(p.lon), f(p.bearing)]


def platform(p: Platform | None) -> list[Any] | None:
    if p is None:
        return None
    return [p.value, p.label, p.boarding_point_id, t(p.assigned_at), p.source]


def call_state(c: CallState) -> list[Any]:
    return [
        c.sequence,
        c.location_id,
        c.visit_n,
        iv(c.arrival),
        iv(c.departure),
        t(c.estimated_arrival),
        t(c.estimated_departure),
        c.status,
        c.source_class,
        t(c.passed_arrival),
        t(c.passed_departure),
        c.passenger,
        platform(c.platform),
    ]


def track(value: Track | None) -> list[Any] | None:
    if value is None:
        return None
    fix = value.fix
    stop = value.at_stop
    off = value.off_path
    return [
        [
            [
                f(h.along_m),
                t(h.at),
                f(h.lateness_s),
                f(h.log_p),
                [[f(p.along_m), t(p.at)] for p in h.history],
                h.at_us,
                t(h.off_path_since),
                f(h.reckoned_m),
            ]
            for h in value.hypotheses
        ],
        f(value.committed_m),
        t(value.unmatched_since),
        t(value.seen_at),
        t(value.crossed_at),
        None if off is None else [f(off.along_m), t(off.at), f(off.lateness_s)],
        None if fix is None else [fix[0], f(fix[1]), f(fix[2])],
        None
        if stop is None
        else [stop.call, t(stop.first), t(stop.last), t(stop.away), f(stop.away_m)],
    ]


def lead(value: Lead | None) -> list[Any] | None:
    if value is None:
        return None
    return [[value.vehicle.source, value.vehicle.source_vehicle_id], t(value.seen)]


def write_event(e: WriteEvent) -> list[Any]:
    return [
        journey(e.journey),
        e.location_id,
        e.visit_n,
        e.kind,
        iv(e.interval),
        e.method,
        e.source,
    ]


def semantics(s: SourceSemantics) -> list[bool]:
    return [
        s.pre_trip_delay_is_elapsed,
        s.pre_trip_after_end_is_stale,
        s.unchanged_entry_is_no_news,
        s.delay_floor_minute,
    ]


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


def trip_json(trip: Trip) -> dict[str, Any]:
    return {
        "trip_id": trip.trip_id,
        "route_id": trip.route_id,
        "route_name": trip.route_name,
        "mode": trip.mode,
        "service_id": trip.service_id,
        "headsign": trip.headsign,
        "shape_id": trip.shape_id,
        "run_key": trip.run_key,
        "run_part": trip.run_part,
        "train_number": trip.train_number,
        "agency_id": trip.agency_id,
        "parts": [[p.trip_id, p.train_number, p.first, p.last] for p in trip.parts],
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
            for c in trip.calls
        ],
    }


def index_json(index: Index) -> dict[str, Any]:
    """What plans and station lookups read of a unit test's index, in the trace format."""

    return {
        "release_id": index.release_id,
        "feed": index.feed,
        "keys": [],
        "trips": [],
        "service_dates": {},
        "loaded_dates": [],
        "observations": [],
        "location_keys": [
            [location_id, namespace, identifier]
            for (namespace, identifier), located in sorted(index.keyed.items())
            for location_id in located
        ],
        "locations": [
            [loc.location_id, loc.name, loc.lon, loc.lat]
            for loc in (index.locations[p] for p in sorted(index.locations))
        ],
        "shapes": [
            {
                "shape_id": s.shape_id,
                "points": [list(p) for p in s.points],
                "distances_m": list(s.distances_m),
            }
            for s in (index.shapes[i] for i in sorted(index.shapes))
        ],
    }


def same_policy(policy: Policy) -> bool:
    return replace(policy, rail=replace(policy.rail, predictor=POLICY.rail.predictor)) == POLICY


# --- recording ----------------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.groups: list[dict[str, Any]] = []
        self.group: dict[str, Any] | None = None
        self.detail = False
        self.corpus: Index | None = None
        self.depth = 0
        self._keep: list[Any] = []
        self._indexes: dict[int, int] = {}
        self._trips: dict[int, int] = {}
        self._trip_texts: dict[str, int] = {}
        self._observations: dict[int, int] = {}
        self._facts: dict[int, int] = {}
        self._last: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, Any] | None = None  # the estimate_all journey being recorded

    def begin(self, name: str, detail: bool, corpus: str | None = None) -> None:
        self.group = {
            "name": name,
            "corpus": corpus,
            "indexes": [],
            "trips": [],
            "observations": [],
            "records": [],
        }
        self.detail = detail
        self.corpus = None
        self._keep = []
        self._indexes, self._trips, self._trip_texts = {}, {}, {}
        self._observations, self._facts, self._last = {}, {}, {}
        self.pending = None

    def end(self) -> None:
        if self.group is not None and self.group["records"]:
            self.groups.append(self.group)
        self.group = None

    def on(self, nested: bool = False) -> bool:
        if self.group is None:
            return False
        return self.depth == 0 or (nested and self.detail)

    def index(self, index: Index) -> int:
        assert self.group is not None
        if self.group["corpus"] is not None:
            if self.corpus is None:
                self.corpus = index
            assert index is self.corpus, "one index per corpus journey"
            return 0
        key = id(index)
        if key not in self._indexes:
            self._keep.append(index)
            self._indexes[key] = len(self.group["indexes"])
            self.group["indexes"].append(index_json(index))
        return self._indexes[key]

    def trip(self, trip: Trip) -> int:
        assert self.group is not None
        key = id(trip)
        if key not in self._trips:
            data = trip_json(trip)
            text = json.dumps(data, sort_keys=True)
            if text not in self._trip_texts:
                self._trip_texts[text] = len(self.group["trips"])
                self.group["trips"].append(data)
            self._keep.append(trip)
            self._trips[key] = self._trip_texts[text]
        return self._trips[key]

    def observation(self, o: Observation) -> int:
        assert self.group is not None
        key = id(o)
        if key not in self._observations:
            self._keep.append(o)
            self._observations[key] = len(self.group["observations"])
            self._facts.setdefault(id(o.facts), self._observations[key])
            self.group["observations"].append(observation_json(o))
        return self._observations[key]

    def source_track(self, s: SourceTrack) -> list[Any]:
        facts: Any = None
        if s.facts is not None:
            k = self._facts.get(id(s.facts))
            facts = ["obs", k] if k is not None else facts_to_json(s.facts)
        np, p = s.next_point, s.prediction
        return [
            s.source,
            t(s.heard_at),
            facts,
            None if np is None else [np.name, np.sr70],
            s.delay_s,
            t(s.delay_at),
            s.delay_reference,
            None if p is None else [p.sr70, t(p.scheduled), t(p.predicted)],
            s.point,
            t(s.position_at),
            lead(s.lead),
        ]

    def full(self, i: Instance) -> dict[str, Any]:
        pub = i.published
        return {
            "journey": journey(i.journey),
            "release": i.release_id,
            "trip": i.trip_id,
            "mode": i.mode,
            "life": i.lifecycle,
            "calls": [call_state(c) for c in i.calls],
            "fresh": [
                t(i.freshness.updated_at),
                t(i.freshness.heard_at),
                i.freshness.stale,
                i.freshness.lost,
            ],
            "prog": None
            if i.progress is None
            else [f(i.progress.distance_m), i.progress.call_index, t(i.progress.at)],
            "track": track(i.track),
            "rep": i.reported_delay_s,
            "late": i.lateness_s,
            "ors": t(i.off_route_since),
            "or": i.off_route,
            "lead": lead(i.lead),
            "journeys": [[journey(s.journey), s.first, s.last] for s in i.journeys],
            "parts": [[s.trip_id, s.first, s.last] for s in i.parts],
            "sources": [self.source_track(s) for s in i.sources],
            "ts": i.track_source,
            "ss": i.shown_source,
            "pub": None
            if pub is None
            else [f(pub.lon), f(pub.lat), f(pub.bearing), t(pub.at), f(pub.along_m), pub.snapped],
            "held": [write_event(e) for e in i.held],
        }

    def instance(self, i: Instance) -> dict[str, Any]:
        """The instance, as the fields changed against its journey's previous one."""

        full = self.full(i)
        key = json.dumps(full["journey"])
        last = self._last.get(key)
        self._last[key] = full
        if last is None:
            return full
        out: dict[str, Any] = {"~": 1, "journey": full["journey"]}
        for name, value in full.items():
            before = last[name]
            if value == before:
                continue
            if name == "calls" and len(value) == len(before):
                out["calls~"] = [None if a == b else a for a, b in zip(value, before, strict=True)]
            else:
                out[name] = value
        return out

    def effects(self, effects: list[Any], observation: Observation) -> list[Any]:
        out: list[Any] = []
        for e in effects:
            if isinstance(e, WriteEvent):
                out.append(["W", *write_event(e)])
            elif isinstance(e, Unresolved):
                assert e.observation is observation
                out.append(["U", e.kind, e.name])
            elif isinstance(e, RecordPlatform):
                out.append(
                    [
                        "P",
                        journey(e.journey),
                        e.location_id,
                        e.visit_n,
                        e.kind,
                        platform(e.platform),
                    ]
                )
            elif isinstance(e, ObservationResult):
                assert e.observation is observation
                out.append(
                    [
                        "R",
                        None if e.journey is None else journey(e.journey),
                        None if e.reason is None else e.reason.value,
                        [journey(j) for j in e.public],
                        t(e.state_since),
                        e.method,
                    ]
                )
            else:
                raise AssertionError(f"unexpected effect {type(e).__name__}")
        return out

    def add(self, record: dict[str, Any]) -> None:
        assert self.group is not None
        self.group["records"].append(record)


REC = Recorder()


def _install() -> None:
    advance = timeline_module.advance

    def recorded_advance(
        instance: Instance,
        trip: Trip,
        observation: Observation,
        index: Any,
        policy: Policy,
        plans: Any,
        sem: SourceSemantics,
        before: Position | None = None,
    ) -> tuple[Instance, list[Any]]:
        if not REC.on():
            return advance(instance, trip, observation, index, policy, plans, sem, before)
        assert same_policy(policy), "advance with another policy"
        REC.depth += 1
        try:
            out, effects = advance(instance, trip, observation, index, policy, plans, sem, before)
        finally:
            REC.depth -= 1
        REC.add(
            {
                "k": "adv",
                "ix": REC.index(index),
                "trip": REC.trip(trip),
                "obs": REC.observation(observation),
                "sem": semantics(sem),
                "before": position(before),
                "in": REC.instance(instance),
                "out": REC.instance(out),
                "fx": REC.effects(effects, observation),
            }
        )
        return out, effects

    timeline_module.advance = recorded_advance  # type: ignore[assignment]

    apply_point = timeline_module.apply_point

    def recorded_point(
        instance: Instance,
        trip: Trip,
        plan: Any,
        observation: Observation,
        previous: SourceTrack | None,
        index: Any,
        policy: Policy,
    ) -> tuple[Instance, list[Any]]:
        out, effects = apply_point(instance, trip, plan, observation, previous, index, policy)
        if REC.group is not None and REC.depth <= 1:
            REC.add(
                {
                    "k": "point",
                    "ix": REC.index(index),
                    "trip": REC.trip(trip),
                    "obs": REC.observation(observation),
                    "prev": None if previous is None else REC.source_track(previous),
                    "in": REC.instance(instance),
                    "out": REC.instance(out),
                    "fx": REC.effects(effects, observation),
                }
            )
        return out, effects

    timeline_module.apply_point = recorded_point  # type: ignore[assignment]

    note_source = timeline_module.note_source

    def recorded_note(
        instance: Instance, observation: Observation, sem: SourceSemantics, delay_s: int | None
    ) -> tuple[SourceTrack, SourceTrack | None, bool]:
        out = note_source(instance, observation, sem, delay_s)
        if REC.on(nested=True):
            new, previous, news = out
            REC.add(
                {
                    "k": "note",
                    "obs": REC.observation(observation),
                    "sem": semantics(sem),
                    "delay": delay_s,
                    "in": REC.instance(instance),
                    "out": [
                        REC.source_track(new),
                        None if previous is None else REC.source_track(previous),
                        news,
                    ],
                }
            )
        return out

    timeline_module.note_source = recorded_note  # type: ignore[assignment]

    usable_delay = timeline_module.usable_delay

    def recorded_usable(
        instance: Instance, observation: Observation, policy: Policy, sem: SourceSemantics
    ) -> int | None:
        out = usable_delay(instance, observation, policy, sem)
        if REC.on(nested=True):
            REC.add(
                {
                    "k": "usable",
                    "obs": REC.observation(observation),
                    "sem": semantics(sem),
                    "in": REC.instance(instance),
                    "out": out,
                }
            )
        return out

    timeline_module.usable_delay = recorded_usable  # type: ignore[assignment]

    with_lead = core_module.with_lead

    def recorded_lead(instance: Instance, vehicle: Any, observation: Observation) -> Instance:
        out = with_lead(instance, vehicle, observation)
        if REC.on():
            REC.add(
                {
                    "k": "lead",
                    "obs": REC.observation(observation),
                    "veh": [vehicle.source, vehicle.source_vehicle_id],
                    "in": REC.instance(instance),
                    "out": REC.instance(out),
                }
            )
        return out

    core_module.with_lead = recorded_lead  # type: ignore[assignment]

    _install_estimate()
    _install_platform()


def _install_estimate() -> None:
    """One `est` record per journey `core.estimate_all` estimates, assembled from the calls it
    makes in order (current_lateness ... monotone)."""

    def start(name: str, make: Any) -> None:
        original = getattr(core_module, name)

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            out = original(*args, **kwargs)
            if REC.group is not None:
                make(REC, out, *args, **kwargs)
            return out

        setattr(core_module, name, wrapped)

    def current(rec: Recorder, out: Any, instance: Instance) -> None:
        rec.pending = {"k": "est", "_in": instance, "cur0": [td(out[0]), out[1]]}

    def waiting(rec: Recorder, out: Any, instance: Instance, plan: Any, cur: Any) -> None:
        assert rec.pending is not None and rec.pending["_in"] is instance

    def credit(rec: Recorder, out: Any, instance: Instance, trip: Trip, *_: Any) -> None:
        assert rec.pending is not None and rec.pending["_in"] is instance
        rec.pending["_trip"] = trip
        rec.pending["cur"] = [td(out[0]), out[1]]

    def anchored(
        rec: Recorder, out: Any, instance: Instance, trip: Trip, index: Any, predictor: str
    ) -> None:
        assert rec.pending is not None
        rec.pending["_index"] = index
        rec.pending["pred"] = predictor
        rec.pending["anchor"] = None if out is None else [out[0], td(out[1])]

    def extra(rec: Recorder, out: Any, *_: Any) -> None:
        assert rec.pending is not None
        rec.pending["extra"] = [] if not any(out) else [td(x) for x in out]

    def fresh(rec: Recorder, out: Any, instance: Instance, now: Instant, policy: Any) -> None:
        assert rec.pending is not None
        rec.pending["now"] = t(now)
        rec.pending["fresh"] = t(out)

    def dwelt(rec: Recorder, out: Any, *_: Any) -> None:
        assert rec.pending is not None
        rec.pending["dwelt"] = None if out is None else [out[0], t(out[1])]

    def left(rec: Recorder, out: Any, *_: Any) -> None:
        assert rec.pending is not None
        rec.pending["left"] = None if out is None else [out[0], t(out[1])]

    def drives(rec: Recorder, out: Any, *_: Any) -> None:
        assert rec.pending is not None
        rec.pending["drives"] = [] if not any(out) else [td(x) for x in out]

    def estimated(rec: Recorder, out: Any, *_: Any, **__: Any) -> None:
        assert rec.pending is not None

    def done(rec: Recorder, out: Instance, estimated_instance: Instance) -> None:
        pending = rec.pending
        assert pending is not None
        rec.pending = None
        if rec.depth != 0:
            return
        record = {
            "k": "est",
            "ix": rec.index(pending.pop("_index")),
            "trip": rec.trip(pending.pop("_trip")),
            "in": rec.instance(pending.pop("_in")),
        }
        record.update(pending)
        record["out"] = rec.instance(out)
        rec.add(record)

    start("current_lateness", current)
    start("waiting_is_not_early", waiting)
    start("dwell_credit", credit)
    start("anchor_prediction", anchored)
    start("running_extra", extra)
    start("_fresh", fresh)
    start("last_fix_at_stop", dwelt)
    start("_seen_leaving", left)
    start("minimum_drives", drives)
    start("estimate", estimated)
    start("monotone", done)


def _install_platform() -> None:
    apply_platform = core_module.apply_platform

    def recorded_platform(
        state: Any, observation: Observation, index: Any, bind: Any, open_instance: Any
    ) -> list[Any]:
        if not REC.on():
            return apply_platform(state, observation, index, bind, open_instance)
        seen: dict[str, Any] = {}

        def bound(o: Observation) -> Any:
            result = bind(o)
            seen["bind"] = result
            return result

        def opened(match: Match) -> list[Any]:
            effects = open_instance(match)
            seen["opened"] = len(effects)
            seen["inst"] = state.instances[match.journey]
            return effects

        effects = apply_platform(state, observation, index, bound, opened)
        result = seen["bind"]
        record: dict[str, Any] = {
            "k": "plat",
            "ix": REC.index(index),
            "obs": REC.observation(observation),
        }
        if isinstance(result, Match):
            record["bind"] = {
                "m": [
                    journey(result.journey),
                    REC.trip(result.trip),
                    t(result.start),
                    t(result.end),
                ]
            }
            record["inst"] = REC.instance(seen["inst"])
            record["opened"] = seen["opened"]
            record["out"] = REC.instance(state.instances[result.journey])
            record["dirty"] = result.journey in state.dirty
        else:
            record["bind"] = {"r": result.value}
            record["opened"] = 0
        record["fx"] = REC.effects(effects[record["opened"] :], observation)
        REC.add(record)
        return effects

    core_module.apply_platform = recorded_platform  # type: ignore[assignment]


# --- runs ---------------------------------------------------------------------------------------


def run_corpus(detail_journeys: int) -> None:
    from tests.realtime import corpus

    for i, name in enumerate(corpus.names()):
        REC.begin(f"corpus/{name}", detail=i < detail_journeys, corpus=name)
        corpus.run(name)
        REC.end()


def run_tests() -> None:
    import pytest

    class Groups:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item: Any) -> Any:
            REC.begin(f"test/{item.nodeid}", detail=True)
            try:
                yield
            finally:
                REC.end()

    code = pytest.main(
        ["-q", "-p", "no:cacheprovider", "--rootdir", str(ROOT), *(str(ROOT / p) for p in TESTS)],
        plugins=[Groups()],
    )
    assert code == 0, f"the unit scenarios failed under the recorder: {code}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detail-journeys", type=int, default=8)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--only-tests", action="store_true")
    args = parser.parse_args()
    _install()
    run_tests()
    if not args.only_tests:
        run_corpus(args.detail_journeys)
    document = {"groups": REC.groups}
    text = json.dumps(document, separators=(",", ":"), allow_nan=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(args.out, "wb", compresslevel=9, mtime=0) as out:
        out.write(text.encode("utf-8"))
    counts: dict[str, int] = {}
    for group in REC.groups:
        for record in group["records"]:
            counts[record["k"]] = counts.get(record["k"], 0) + 1
    print(f"{len(REC.groups)} groups, records {dict(sorted(counts.items()))}")
    print(f"{len(text):,} bytes of JSON, {args.out.stat().st_size:,} gzipped -> {args.out}")


if __name__ == "__main__":
    main()
