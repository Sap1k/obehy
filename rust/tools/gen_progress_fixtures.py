"""Writes the Python reference fixture for the Rust ports of `timeline/plan.py`, `stops.py`,
`commit.py` and `progress.py` (docs/RUST_PORT.md, R4).

    uv run python rust/tools/gen_progress_fixtures.py [--detail-journeys N] [--out PATH]

Nothing is re-implemented here: the real core runs and every call into the four modules from
outside them is recorded with its inputs and its output, at full precision:

- the corpus journeys (`tests/realtime/corpus.py`: about a hundred real journeys of every
  source and mode, each with its static slice from a release's serving files), stepped through
  the core with the replay's emit ticks;
- the unit scenarios of `tests/unit/realtime/test_progress.py`, `test_stops.py` and
  `test_timeline.py`, run under pytest in this process.

Recorded per group (a journey or a test): every `progress.update`, `at_origin_or_moving`,
`past_first_stop`, `commit.terminus` and `stops.departure` call the core makes; in the first
`--detail-journeys` journeys and in every test also the calls `update` makes (`commit.commit`,
`stops.seen`, `capped`, `done_at`). Each plan a call reads is written once per group: its path
(the simplified one `build_path` made; path.rs is checked against `build_path` on its own), its
schedule (and the trip's schedule seconds and service date, for `scheduled_instants`), and what
`build_plan` made of it: zones, triggers, and the timetable and plan queries at probe points.

A track passed to `update` that is the very object an earlier `update` returned is written as a
reference to that call (`["ref", k]`), so the Rust test feeds its own output back in, as the core
does. Floats are JSON numbers (repr, exact); non-finite ones the strings "inf", "-inf", "nan";
instants are integer microseconds since the epoch. Output: `progress.json.gz`.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from obehy.realtime import core as core_module  # noqa: E402
from obehy.realtime import timeline as timeline_module  # noqa: E402
from obehy.realtime.facts import Interval, Position  # noqa: E402
from obehy.realtime.model import (  # noqa: E402
    Hypothesis,
    OffPath,
    Placement,
    StopSeen,
    Track,
)
from obehy.realtime.policy import ProgressPolicy, load_policy  # noqa: E402
from obehy.realtime.timeline import commit as commit_module  # noqa: E402
from obehy.realtime.timeline import progress as progress_module  # noqa: E402
from obehy.realtime.timeline import stops as stops_module  # noqa: E402
from obehy.realtime.timeline.commit import Committed, Crossing  # noqa: E402
from obehy.realtime.timeline.plan import Plan  # noqa: E402
from obehy.realtime.times import Instant, micros  # noqa: E402

OUT = ROOT / "rust" / "obehy-core" / "tests" / "fixtures" / "progress.json.gz"
POLICY = load_policy()
BASE = POLICY.progress
TESTS = (
    "tests/unit/realtime/test_progress.py",
    "tests/unit/realtime/test_stops.py",
    "tests/unit/realtime/test_timeline.py",
)
PROBE_TIMES = 3


# --- serialization ------------------------------------------------------------------------------


def f(x: float | None) -> float | str | None:
    if x is None:
        return None
    if math.isfinite(x):
        return x
    return "nan" if math.isnan(x) else ("inf" if x > 0 else "-inf")


def t(at: Instant | None) -> int | None:
    return None if at is None else micros(at)


def interval(value: Interval | None) -> list[Any] | None:
    return None if value is None else [t(value.lo), t(value.hi)]


def placement(p: Placement) -> list[Any]:
    return [f(p.along_m), t(p.at)]


def hypothesis(h: Hypothesis) -> list[Any]:
    return [
        f(h.along_m),
        t(h.at),
        f(h.lateness_s),
        f(h.log_p),
        [placement(p) for p in h.history],
        h.at_us,
        t(h.off_path_since),
        f(h.reckoned_m),
    ]


def off_path(o: OffPath | None) -> list[Any] | None:
    return None if o is None else [f(o.along_m), t(o.at), f(o.lateness_s)]


def stop_seen(s: StopSeen | None) -> list[Any] | None:
    if s is None:
        return None
    return [s.call, t(s.first), t(s.last), t(s.away), f(s.away_m)]


def track(value: Track | None) -> list[Any] | None:
    if value is None:
        return None
    fix = value.fix
    return [
        [hypothesis(h) for h in value.hypotheses],
        f(value.committed_m),
        t(value.unmatched_since),
        t(value.seen_at),
        t(value.crossed_at),
        off_path(value.off_path),
        None if fix is None else [fix[0], f(fix[1]), f(fix[2])],
        stop_seen(value.at_stop),
    ]


def position(p: Position | None) -> list[Any] | None:
    return None if p is None else [f(p.lat), f(p.lon), f(p.bearing)]


def crossing(c: Crossing) -> list[Any]:
    return [c.call, c.kind, interval(c.fixes), t(c.at), c.method, interval(c.interval)]


def committed(c: Committed) -> list[Any]:
    return [
        [hypothesis(h) for h in c.beam],
        f(c.committed_m),
        [crossing(x) for x in c.crossed],
        t(c.crossed_at),
    ]


def step_out(s: progress_module.Step) -> list[Any]:
    p = s.progress
    return [
        track(s.track),
        None if p is None else [f(p.distance_m), p.call_index, t(p.at)],
        [crossing(c) for c in s.crossed],
        t(s.off_route_since),
        s.off_route,
    ]


def sigma_of(policy: ProgressPolicy) -> float:
    """The progress policy's GPS sigma; every other field is the loaded policy's."""

    assert dataclasses.replace(policy, gps_sigma_m=BASE.gps_sigma_m) == BASE
    return policy.gps_sigma_m


# --- plans --------------------------------------------------------------------------------------


def probes(plan: Plan, rng: random.Random) -> dict[str, Any]:
    """The timetable and plan queries at points along the path and times around the schedule."""

    path = plan.path
    length = path.length_m
    alongs: list[float] = [-10.0, 0.0, length, length + 10.0]
    for d in (*path.call_distances_m, *plan.trigger_distances):
        alongs.extend((math.nextafter(d, -math.inf), d, math.nextafter(d, math.inf)))
    for a, b in zip(path.call_distances_m, path.call_distances_m[1:], strict=False):
        alongs.append((a + b) / 2)
    alongs.extend(rng.uniform(-50.0, length + 50.0) for _ in range(8))
    times = [x for pair in plan.scheduled for x in pair if x is not None]
    moments: list[Instant] = []
    if times:
        lo, hi = min(times), max(times)
        moments = [lo - (hi - lo) / 4, lo + (hi - lo) * 0.37, hi + (hi - lo) / 3]
        moments.append(lo + (hi - lo) * rng.random())
    moments = moments[: PROBE_TIMES + 1]
    tt = plan.timetable
    rows: list[list[Any]] = []
    for along in alongs:
        window = tt.window(along)
        rows.append(
            [
                f(along),
                None if window is None else [t(window.earliest), t(window.latest)],
                [f(tt.lateness_s(m, along)) for m in moments],
                [t(tt.planned(m, along)) for m in moments],
                plan.call_index_at(along),
                plan.at_stop(along),
                plan.dwelling_call(along),
            ]
        )
    between: list[list[Any]] = []
    for _ in range(12):
        a, b = rng.choice(alongs), rng.choice(alongs)
        found = plan.triggers_between(a, b)
        first = plan.triggers.index(found[0]) if found else None
        between.append([f(a), f(b), len(found), first])
    for a in (-math.inf, *path.call_distances_m[:1]):
        found = plan.triggers_between(a, math.inf)
        between.append([f(a), "inf", len(found), 0 if found else None])
    return {
        "moments": [t(m) for m in moments],
        "rows": rows,
        "between": between,
        "departure_m": f(plan.departure_m) if path.call_distances_m else None,
        "leaving": [f(plan.leaving_m(i)) for i in range(len(plan.zones))],
        "scheduled_time": [
            [t(plan.scheduled_time(i, "arrival")), t(plan.scheduled_time(i, "departure"))]
            for i in range(len(plan.zones))
        ],
    }


def plan_record(
    plan: Plan, origin: tuple[str, str, list[list[int | None]]] | None
) -> dict[str, Any]:
    path = plan.path
    rng = random.Random(len(path.points) * 7919 + len(plan.zones))
    return {
        "trip": None if origin is None else [origin[0], origin[1], origin[2]],
        "path": [
            [[f(lon), f(lat)] for lon, lat in path.points],
            [f(d) for d in path.distances_m],
            [f(d) for d in path.call_distances_m],
            path.shaped,
        ],
        "scheduled": [[t(a), t(d)] for a, d in plan.scheduled],
        "zones": [[f(z.arrival_m), f(z.departure_m)] for z in plan.zones],
        "triggers": [[f(x.along_m), x.call, x.kind] for x in plan.triggers],
        "probes": probes(plan, rng),
    }


# --- recording ----------------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.groups: list[dict[str, Any]] = []
        self.group: dict[str, Any] | None = None
        self.detail = False
        self.depth = 0
        # Each plan's trip and service date by plan identity; holds the plan so ids stay unique.
        self.origins: dict[int, tuple[tuple[str, str, list[list[int | None]]], Plan]] = {}
        self._plans: dict[int, int] = {}
        self._tracks: dict[int, int] = {}
        self._keep: list[Any] = []
        self._departures: set[str] = set()

    def begin(self, name: str, detail: bool) -> None:
        self.group = {"name": name, "plans": [], "calls": []}
        self.detail = detail
        self._plans, self._tracks, self._keep = {}, {}, []
        self._departures = set()

    def end(self) -> None:
        if self.group is not None and self.group["calls"]:
            self.groups.append(self.group)
        self.group = None

    def plan(self, plan: Plan) -> int:
        assert self.group is not None
        key = id(plan)
        if key not in self._plans:
            self._keep.append(plan)
            self._plans[key] = len(self.group["plans"])
            origin = self.origins.get(key)
            self.group["plans"].append(plan_record(plan, None if origin is None else origin[0]))
        return self._plans[key]

    def track_in(self, value: Track | None) -> Any:
        if value is not None and id(value) in self._tracks:
            return ["ref", self._tracks[id(value)]]
        return track(value)

    def add(self, call: list[Any], output: Any = None) -> int:
        assert self.group is not None
        calls = self.group["calls"]
        calls.append(call)
        if output is not None:
            self._keep.append(output)
            self._tracks[id(output)] = len(calls) - 1
        return len(calls) - 1

    def recording(self, nested: bool) -> bool:
        if self.group is None:
            return False
        return self.depth == 0 or (nested and self.detail)


REC = Recorder()


def _wrap_plan_cache() -> None:
    """Remember each plan's trip and service date (for `scheduled_instants`)."""

    from obehy.realtime.timeline.plan import PlanCache

    original = PlanCache.plan

    def plan(self: PlanCache, trip: Any, day: Any, index: Any, policy: Any) -> Plan:
        value = original(self, trip, day, index, policy)
        if id(value) not in REC.origins:
            seconds = [[c.arrival, c.departure] for c in trip.calls]
            REC.origins[id(value)] = ((trip.trip_id, str(day), seconds), value)
        return value

    PlanCache.plan = plan  # type: ignore[method-assign]


def _install() -> None:
    _wrap_plan_cache()
    update = progress_module.update

    def recorded_update(
        plan: Plan,
        track_value: Track | None,
        pos: Position,
        at: Instant,
        mode: str,
        source: str,
        policy: ProgressPolicy,
        dwell_s: float,
        stop_radius_m: float = 0.0,
        terminal_m: float = 0.0,
        prior: Position | None = None,
    ) -> progress_module.Step:
        if not REC.recording(nested=False):
            return update(
                plan, track_value, pos, at, mode, source, policy, dwell_s,
                stop_radius_m, terminal_m, prior,
            )  # fmt: skip
        head = [
            "update",
            REC.plan(plan),
            REC.track_in(track_value),
            position(pos),
            t(at),
            mode,
            source,
            sigma_of(policy),
            f(dwell_s),
            f(stop_radius_m),
            f(terminal_m),
            position(prior),
        ]
        REC.depth += 1
        try:
            out = update(
                plan, track_value, pos, at, mode, source, policy, dwell_s,
                stop_radius_m, terminal_m, prior,
            )  # fmt: skip
        finally:
            REC.depth -= 1
        REC.add([*head, step_out(out)], out.track)
        return out

    timeline_module.update = recorded_update  # type: ignore[assignment]

    commit = commit_module.commit

    def recorded_commit(
        plan: Plan,
        beam: tuple[Hypothesis, ...],
        committed_m: float,
        crossed_at: Instant | None,
        at: Instant,
        policy: ProgressPolicy,
        dwell_s: float,
        *,
        stop: StopSeen | None = None,
        returning: bool = False,
    ) -> Committed:
        out = commit(
            plan, beam, committed_m, crossed_at, at, policy, dwell_s,
            stop=stop, returning=returning,
        )  # fmt: skip
        if REC.recording(nested=True):
            REC.add(
                [
                    "commit",
                    REC.plan(plan),
                    [hypothesis(h) for h in beam],
                    f(committed_m),
                    t(crossed_at),
                    t(at),
                    sigma_of(policy),
                    f(dwell_s),
                    stop_seen(stop),
                    returning,
                    committed(out),
                ]
            )
        return out

    progress_module.commit = recorded_commit  # type: ignore[assignment]

    seen = stops_module.seen

    def recorded_seen(
        plan: Plan,
        previous: StopSeen | None,
        committed_m: float,
        here: tuple[float, float],
        at: Instant,
        radius_m: float,
        ahead: int,
        terminal_m: float = 0.0,
        still_since: Instant | None = None,
    ) -> StopSeen | None:
        out = seen(plan, previous, committed_m, here, at, radius_m, ahead, terminal_m, still_since)
        if REC.recording(nested=True):
            REC.add(
                [
                    "seen",
                    REC.plan(plan),
                    stop_seen(previous),
                    f(committed_m),
                    [f(here[0]), f(here[1])],
                    t(at),
                    f(radius_m),
                    ahead,
                    f(terminal_m),
                    t(still_since),
                    stop_seen(out),
                ]
            )
        return out

    progress_module.seen = recorded_seen  # type: ignore[assignment]

    capped = stops_module.capped

    def recorded_capped(plan: Plan, value: StopSeen | None, along_m: float) -> float:
        out = capped(plan, value, along_m)
        if REC.recording(nested=True):
            REC.add(["capped", REC.plan(plan), stop_seen(value), f(along_m), f(out)])
        return out

    progress_module.capped = recorded_capped  # type: ignore[assignment]

    done_at = stops_module.done_at

    def recorded_done_at(plan: Plan, call: int, committed_m: float) -> bool:
        out = done_at(plan, call, committed_m)
        if REC.recording(nested=True):
            REC.add(["done_at", REC.plan(plan), call, f(committed_m), out])
        return out

    progress_module.done_at = recorded_done_at  # type: ignore[assignment]

    terminus = commit_module.terminus

    def recorded_terminus(
        plan: Plan, history: tuple[Placement, ...], policy: ProgressPolicy
    ) -> Interval | None:
        out = terminus(plan, history, policy)
        if REC.recording(nested=False):
            REC.add(
                [
                    "terminus",
                    REC.plan(plan),
                    [placement(p) for p in history],
                    sigma_of(policy),
                    interval(out),
                ]
            )
        return out

    core_module.terminus = recorded_terminus  # type: ignore[assignment]

    departure = stops_module.departure

    def recorded_departure(plan: Plan, value: StopSeen, dwell_s: float) -> Instant:
        out = departure(plan, value, dwell_s)
        if REC.recording(nested=False):
            call = ["departure", REC.plan(plan), stop_seen(value), f(dwell_s), t(out)]
            key = json.dumps(call)
            if key not in REC._departures:  # pyright: ignore[reportPrivateUsage]
                REC._departures.add(key)  # pyright: ignore[reportPrivateUsage]
                REC.add(call)
        return out

    core_module.departure = recorded_departure  # type: ignore[assignment]

    origin = progress_module.at_origin_or_moving

    def recorded_origin(
        plan: Plan,
        before: Position | None,
        after: Position,
        at: Instant,
        policy: ProgressPolicy,
        terminal_m: float = 0.0,
    ) -> bool:
        out = origin(plan, before, after, at, policy, terminal_m)
        if REC.recording(nested=False):
            REC.add(
                [
                    "origin",
                    REC.plan(plan),
                    position(before),
                    position(after),
                    t(at),
                    sigma_of(policy),
                    f(terminal_m),
                    out,
                ]
            )
        return out

    core_module.at_origin_or_moving = recorded_origin  # type: ignore[assignment]

    past = progress_module.past_first_stop

    def recorded_past(plan: Plan, pos: Position, policy: ProgressPolicy) -> bool:
        out = past(plan, pos, policy)
        if REC.recording(nested=False):
            REC.add(["past", REC.plan(plan), position(pos), sigma_of(policy), out])
        return out

    core_module.past_first_stop = recorded_past  # type: ignore[assignment]


# --- runs ---------------------------------------------------------------------------------------


def run_corpus(detail_journeys: int) -> None:
    from tests.realtime import corpus

    for i, name in enumerate(corpus.names()):
        REC.begin(f"corpus/{name}", detail=i < detail_journeys)
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
    parser.add_argument("--detail-journeys", type=int, default=12)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--only-tests", action="store_true")
    args = parser.parse_args()
    _install()
    run_tests()
    if not args.only_tests:
        run_corpus(args.detail_journeys)
    document = {
        "policy": {"gps_sigma_m": BASE.gps_sigma_m},
        "groups": REC.groups,
    }
    text = json.dumps(document, separators=(",", ":"), allow_nan=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(args.out, "wb", compresslevel=9, mtime=0) as out:
        out.write(text.encode("utf-8"))
    counts: dict[str, int] = {}
    for group in REC.groups:
        for call in group["calls"]:
            counts[call[0]] = counts.get(call[0], 0) + 1
    plans = sum(len(g["plans"]) for g in REC.groups)
    print(f"{len(REC.groups)} groups, {plans} plans, calls {dict(sorted(counts.items()))}")
    print(f"{len(text):,} bytes of JSON, {args.out.stat().st_size:,} gzipped -> {args.out}")


if __name__ == "__main__":
    main()
