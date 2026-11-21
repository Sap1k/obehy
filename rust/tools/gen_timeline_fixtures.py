"""Writes the Python reference fixtures for the Rust ports of `timeline/path.py` and
`timeline/runtimes.py` (docs/RUST_PORT.md, R4).

    uv run python rust/tools/gen_timeline_fixtures.py [release-dir]

Everything is computed by the Python modules themselves (path.py, runtimes.py, geo.py and
`shapely.simplify`, i.e. GEOS), never re-implemented here:

- `path.json`: real trips of a release's serving Parquet (both feeds, loaded through the
  index's own `IndexLoader` over `FileRows`), each with its stop locations, schedule times and
  shape; `build_path` on it with and without the shape, and `project_all`, `point_at`,
  `heading_at` and `candidates` at query points around it. GEOS Douglas-Peucker outputs at
  several tolerances on every shape, and on hand-made edge cases.
- `libm.json`: every distinct argument path.py, geo.py and runtimes.py passed to a `math`
  function while the above ran (sin, cos, asin, sqrt, atan2, exp, hypot), with Python's result:
  the Rust side checks its libm against them.
- `runtimes.json`: a replayed log of `record`, `ratio`, `forget`, `running_extra` and
  `minimum_drives` over random journeys sharing hops.

Large arrays are compared through a 64-bit FNV-1a fold over the IEEE bits of the values (`fnv`
here, the same in the Rust tests); everything else is written as JSON numbers (repr, exact).
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
import struct
import sys
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import shapely
from shapely.geometry import LineString

from obehy.realtime import geo
from obehy.realtime.facts import Interval
from obehy.realtime.index import IndexMiss
from obehy.realtime.index_sql import IndexLoader, ReleaseLoads
from obehy.realtime.model import CallState
from obehy.realtime.policy import load_policy
from obehy.realtime.timeline import path as path_module
from obehy.realtime.timeline import progress as progress_module
from obehy.realtime.timeline import runtimes as runtimes_module
from obehy.realtime.timeline.path import (
    build_path,
    candidates,
    heading_at,
    point_at,
    project_all,
)
from obehy.realtime.times import Instant, micros

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "rust" / "obehy-core" / "tests" / "fixtures"
RELEASE = ROOT / "data" / "releases" / "20261015T014009Z-f4453d48b8d2"
TRIPS = {"jdf": 110, "czptt": 50}
MAX_SHAPE_POINTS = 2500
QUERIES = 6
LIBM_SAMPLES = 6_000

MASK = (1 << 64) - 1


def fnv(values: Any) -> str:
    """FNV-1a over the IEEE bits of each float (one 64-bit word per value)."""

    h = 0xCBF29CE484222325
    for v in values:
        h ^= struct.unpack("<Q", struct.pack("<d", v))[0]
        h = (h * 0x100000001B3) & MASK
    return f"{h:016x}"


def bits(x: float) -> str:
    return f"{struct.unpack('<Q', struct.pack('<d', x))[0]:016x}"


# --- libm recording ---------------------------------------------------------------------------

LIBM: dict[str, set[tuple[float, ...]]] = {
    n: set() for n in ("sin", "cos", "asin", "sqrt", "atan2", "exp", "hypot")
}


class _Recorder:
    def __init__(self, module: Any) -> None:
        self._module = module

    def __getattr__(self, name: str) -> Any:
        f = getattr(self._module, name)
        if name not in LIBM:
            return f

        def call(*args: float) -> float:
            LIBM[name].add(tuple(float(a) for a in args))
            return f(*args)

        return call


for module in (path_module, geo, runtimes_module, progress_module):
    module.math = _Recorder(math)  # type: ignore[attr-defined]


# --- path -------------------------------------------------------------------------------------


def _pair(p: tuple[float, float] | None) -> list[float] | None:
    return None if p is None else [p[0], p[1]]


def _path_out(path: path_module.Path) -> dict[str, Any]:
    return {
        "n": len(path.points),
        "points": fnv(c for p in path.points for c in p),
        "distances": fnv(path.distances_m),
        "segments": fnv(c for s in path.segments for c in s),
        "call_distances": list(path.call_distances_m),
        "shaped": path.shaped,
    }


def _queries(
    rng: random.Random,
    path: path_module.Path,
    located: list[tuple[float, float] | None],
    sigma: Any,
    reach: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    points = path.points
    if len(points) < 2:
        return out
    stops = [p for p in located if p is not None]
    for q in range(QUERIES):
        i = rng.randrange(len(points))
        lon, lat = points[i]
        if q in (1, 2, 3):
            scale = (50.0, 300.0, 2000.0)[q - 1]
            lon += rng.gauss(0, scale) * 1.4e-5
            lat += rng.gauss(0, scale) * 9.0e-6
        elif q == 4 and stops:
            lon, lat = rng.choice(stops)
        elif q == 5:
            j = rng.randrange(len(points) - 1)
            (ax, ay), (bx, by) = points[j], points[j + 1]
            t = rng.random()
            lon = ax + t * (bx - ax) + rng.gauss(0, 10) * 1.4e-5
            lat = ay + t * (by - ay) + rng.gauss(0, 10) * 9.0e-6
        projections = project_all(path, lon, lat)
        nearest = min(projections, key=lambda p: p.lateral_m)
        found = candidates(path, lon, lat, sigma, reach)
        window = (nearest.along_m - 400.0, nearest.along_m + 400.0)
        windowed = candidates(path, lon, lat, sigma, reach, window)
        out.append(
            {
                "lon": lon,
                "lat": lat,
                "project_all": fnv(
                    c for p in projections for c in (p.along_m, p.lateral_m, p.segment_m)
                ),
                "nearest": [nearest.along_m, nearest.lateral_m, nearest.segment_m],
                "candidates": [
                    [c.along_m, c.lateral_m, c.sigma_m, bits(c.heading_deg)] for c in found
                ],
                "window": list(window),
                "windowed": [
                    [c.along_m, c.lateral_m, c.sigma_m, bits(c.heading_deg)] for c in windowed
                ],
            }
        )
    length = path.length_m
    alongs = [-5.0, 0.0, length, length + 3.0, rng.uniform(0, length), rng.uniform(0, length)]
    alongs.append(path.distances_m[rng.randrange(len(path.distances_m))])
    alongs.append(path.call_distances_m[rng.randrange(len(path.call_distances_m))])
    out.append(
        {
            "alongs": alongs,
            "points": [list(point_at(path, a)) for a in alongs],
            "headings": [bits(heading_at(path, a)) for a in alongs],
        }
    )
    return out


def _trip_ids(release: Path, feed: str, count: int, rng: random.Random) -> list[str]:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    serving = release / feed / "serving"
    trips = pq.read_table(serving / "trip.parquet", columns=["trip_id", "shape_id"])
    trips = trips.filter(pc.is_valid(trips.column("shape_id")))
    sizes = pq.read_table(serving / "shape_point.parquet", columns=["shape_id"])
    counts = sizes.group_by("shape_id").aggregate([("shape_id", "count")])
    size = dict(zip(counts.column(0).to_pylist(), counts.column(1).to_pylist(), strict=True))
    by_shape: dict[str, str] = {}
    for trip_id, shape_id in zip(
        trips.column("trip_id").to_pylist(), trips.column("shape_id").to_pylist(), strict=True
    ):
        if size.get(shape_id, 0) <= MAX_SHAPE_POINTS:
            by_shape.setdefault(shape_id, trip_id)
    shapes = sorted(by_shape)
    return sorted(by_shape[s] for s in rng.sample(shapes, min(count, len(shapes))))


def _dp(points: tuple[tuple[float, float], ...], tolerance: float) -> list[tuple[float, float]]:
    kept = shapely.simplify(LineString(points), tolerance, preserve_topology=False)
    return [(float(x), float(y)) for x, y in kept.coords]


def _dp_cases() -> list[dict[str, Any]]:
    """Hand-made lines for the GEOS port's corners: closed lines, collinear and repeated
    points, ties in the farthest distance, distances exactly at the tolerance."""

    lines: list[tuple[list[tuple[float, float]], float]] = [
        ([(0, 0), (1, 10), (0, 20)], 0.9),
        ([(0, 0), (1, 10), (0, 20)], 1.0),
        ([(0, 0), (1, 0), (2, 0), (3, 0)], 0.0),
        ([(0, 0), (1, 1), (2, 0), (1, -1), (0, 0)], 0.5),
        ([(0, 0), (1, 1), (2, 0), (1, -1), (0, 0)], 5.0),
        ([(0, 0), (0, 0), (0, 0)], 0.0),
        ([(0, 0), (1, 1), (0, 0)], 2.0),
        ([(0, 0), (1, 1), (0, 0)], 1.0),
        ([(0, 0), (1, 1), (2, -1), (3, 0)], 1.0),
        ([(0, 0), (1, 1), (2, 1), (3, 0)], 0.5),
        ([(0, 0), (1, 2), (2, 2), (3, 0)], 2.0),
        ([(0, 0), (5, 1), (5, 1), (10, 0)], 0.5),
        ([(0, 0), (-1, 1), (11, 1), (10, 0)], 0.5),
        ([(14.1, 50.1), (14.10001, 50.10002), (14.1, 50.1), (14.2, 50.1)], 1e-5),
        ([(0, 0), (1, 0), (0, 0), (1, 0)], 0.1),
    ]
    out = []
    for coords, tolerance in lines:
        points = tuple((float(x), float(y)) for x, y in coords)
        out.append(
            {
                "points": [list(p) for p in points],
                "tolerance": tolerance,
                "kept": [list(p) for p in _dp(points, tolerance)],
                "simplified": (
                    lambda r: {"points": [list(p) for p in r[0]], "distances": list(r[1])}
                )(
                    path_module.simplify(
                        points, tuple(float(i) for i in range(len(points))), tolerance
                    )
                ),
            }
        )
    return out


def path_fixture(release: Path, policy: Any) -> dict[str, Any]:
    rng = random.Random(20261121)
    run_id = json.loads((release / "release.json").read_text(encoding="utf-8"))["run_id"]
    loads = ReleaseLoads(run_id, {}, files=release)
    sigma = progress_module._sigma(policy.progress)  # pyright: ignore[reportPrivateUsage]
    reach = policy.progress.reach_sigmas
    tolerance = policy.progress.shape_simplify_deg
    shapes: dict[str, dict[str, Any]] = {}
    trips: list[dict[str, Any]] = []
    for feed, count in TRIPS.items():
        loader = IndexLoader(None, loads, feed)  # type: ignore[arg-type]
        ids = _trip_ids(release, feed, count, rng)
        loader._load_trips(ids)  # pyright: ignore[reportPrivateUsage]
        index = loader.index
        for trip_id in ids:
            trip = index.trips.get(trip_id)
            if trip is None or trip.shape_id is None:
                continue
            located = []
            for call in trip.calls:
                location = index.location(call.placed_at)
                ok = location.lon is not None and location.lat is not None
                located.append((location.lon, location.lat) if ok else None)
            times: list[int | None] = []
            for call in trip.calls:
                try:
                    times.append(call.time)
                except IndexMiss:
                    times.append(None)
            shape = index.shape(trip.shape_id)
            if trip.shape_id not in shapes:
                shapes[trip.shape_id] = {
                    "points": [list(p) for p in shape.points],
                    "distances": fnv(shape.distances_m),
                    "dp": [
                        {
                            "tolerance": t,
                            "n": len(kept := _dp(shape.points, t)),
                            "kept": fnv(c for p in kept for c in p),
                        }
                        for t in (tolerance, tolerance * 0.1, tolerance * 10, 0.0)
                    ]
                    if len(shape.points) >= 2
                    else [],
                }
            entry: dict[str, Any] = {
                "feed": feed,
                "trip_id": trip_id,
                "shape_id": trip.shape_id,
                "located": [_pair(p) for p in located],
                "times": times,
            }
            for variant, t in (
                ("shaped", trip),
                ("straight", dataclasses.replace(trip, shape_id=None)),
            ):
                try:
                    built = build_path(t, index, policy.progress)
                except IndexMiss:
                    entry[variant] = {"error": True}
                    continue
                entry[variant] = {
                    **_path_out(built),
                    "queries": _queries(rng, built, located, sigma, reach),
                }
            trips.append(entry)
    return {
        "geos": shapely.geos_version_string,
        "release": run_id,
        "policy": {
            "shape_simplify_deg": tolerance,
            "stop_off_shape_m": policy.progress.stop_off_shape_m,
            "reach_sigmas": reach,
        },
        "dp_cases": _dp_cases(),
        "shapes": shapes,
        "trips": trips,
    }


# --- runtimes ---------------------------------------------------------------------------------

T0 = 1_791_000_000_000_000  # 2026-10-03 UTC, in microseconds


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _at(us: int) -> Instant:
    return _EPOCH + timedelta(microseconds=us)


def _interval_json(value: Interval | None) -> list[int] | None:
    return None if value is None else [micros(value.lo), micros(value.hi)]


def _calls_json(calls: tuple[CallState, ...]) -> list[list[Any]]:
    return [
        [c.location_id, c.passenger, _interval_json(c.arrival), _interval_json(c.departure)]
        for c in calls
    ]


def _hops_json(hops: dict[Any, deque[Any]]) -> list[Any]:
    return [
        [list(key), [[micros(end), taken, planned] for end, taken, planned in traversals]]
        for key, traversals in hops.items()
    ]


def runtimes_fixture(policy: Any) -> dict[str, Any]:
    rng = random.Random(7)
    prediction = policy.prediction
    hops: dict[Any, deque[Any]] = {}
    pool = [f"jdf:S{i}" for i in range(7)]
    log: list[dict[str, Any]] = []
    clock = T0
    routes = []
    for _ in range(6):
        n = rng.randint(4, 9)
        stops = [rng.choice(pool) for _ in range(n)]
        routes.append(stops)
    for journey in range(100):
        mode = rng.choice(["bus", "bus", "tram", "rail"])
        stops = routes[rng.randrange(len(routes))]
        n = len(stops)
        passenger = [rng.random() < 0.85 for _ in range(n)]
        start = clock + rng.randint(-600, 3600) * 1_000_000
        sched: list[tuple[int | None, int | None]] = []
        t = start
        for i in range(n):
            t += rng.choice([0, 60, 60, 120, 180, 240]) * 1_000_000
            arrival = None if i == 0 or rng.random() < 0.05 else t
            dwell = rng.choice([0, 0, 30, 60]) * 1_000_000
            departure = None if i == n - 1 or rng.random() < 0.05 else t + dwell
            t += dwell
            sched.append((arrival, departure))
        scheduled = tuple(
            (None if a is None else _at(a), None if d is None else _at(d)) for a, d in sched
        )
        calls = tuple(CallState(i, stops[i], 1, passenger=passenger[i]) for i in range(n))
        lateness = rng.randint(-60, 600) * 1_000_000
        speed = rng.uniform(0.6, 1.8)
        for i in range(n):
            before = calls
            planned = sched[i][0] if sched[i][0] is not None else sched[i][1]
            if planned is None or rng.random() < 0.1:
                continue
            at = planned + lateness + int((planned - start) * (speed - 1.0))
            spread = rng.choice([0, 0, 1, 30_000_000, 1_000_001])
            interval = Interval(_at(at), _at(at + spread))
            new = list(calls)
            if i == 0:
                new[i] = dataclasses.replace(new[i], departure=interval)
            else:
                new[i] = dataclasses.replace(new[i], arrival=interval)
                if rng.random() < 0.5 and i < n - 1:
                    new[i] = dataclasses.replace(
                        new[i], departure=Interval(_at(at + 30_000_000), _at(at + 30_000_000))
                    )
            calls = tuple(new)
            instance = SimpleNamespace(mode=mode, calls=calls, progress=None)
            runtimes_module.record(hops, instance, before, scheduled, prediction)  # type: ignore[arg-type]
            log.append(
                {
                    "op": "record",
                    "mode": mode,
                    "before": _calls_json(before),
                    "calls": _calls_json(calls),
                    "scheduled": [[a, d] for a, d in sched],
                }
            )
        clock += rng.randint(60, 900) * 1_000_000
        now = clock + rng.randint(-1800, 1800) * 1_000_000
        if journey % 5 == 0:
            log.append(
                {
                    "op": "ratios",
                    "now": now,
                    "ratios": [
                        [list(k), runtimes_module.ratio(hops, k, _at(now), prediction)]
                        for k in hops
                    ],
                    "hops": _hops_json(hops) if journey % 20 == 0 else None,
                }
            )
        if journey % 3 == 0:
            reached = rng.randint(-1, n - 1)
            progress = (
                None
                if rng.random() < 0.2
                else SimpleNamespace(call_index=reached, distance_m=rng.uniform(0, 5000.0))
            )
            distances = sorted(rng.uniform(0, 8000.0) for _ in range(n))
            if rng.random() < 0.3:
                distances[rng.randrange(n)] = distances[0]
                distances.sort()
            zones = [
                SimpleNamespace(
                    arrival_m=None if i == 0 else distances[i] - rng.choice([0.0, 50.0]),
                    departure_m=None if i == n - 1 else distances[i] + rng.choice([0.0, 30.0]),
                )
                for i in range(n)
            ]
            plan = SimpleNamespace(
                scheduled=scheduled,
                zones=zones,
                path=SimpleNamespace(call_distances_m=tuple(distances)),
            )
            instance = SimpleNamespace(mode=mode, calls=calls, progress=progress)
            speed_mps = rng.choice([8.0, 16.7, 33.3])
            extra = runtimes_module.running_extra(instance, plan, hops, _at(now), prediction)  # type: ignore[arg-type]
            drives = runtimes_module.minimum_drives(
                instance,  # type: ignore[arg-type]
                plan,  # type: ignore[arg-type]
                hops,
                _at(now),
                prediction,
                speed_mps,
            )
            log.append(
                {
                    "op": "plan",
                    "now": now,
                    "mode": mode,
                    "calls": _calls_json(calls),
                    "progress": None
                    if progress is None
                    else [progress.call_index, progress.distance_m],
                    "scheduled": [[a, d] for a, d in sched],
                    "zones": [[z.arrival_m, z.departure_m] for z in zones],
                    "call_distances": distances,
                    "speed_mps": speed_mps,
                    "extra": [e // timedelta(microseconds=1) for e in extra],
                    "drives": [d // timedelta(microseconds=1) for d in drives],
                }
            )
        if journey % 17 == 16:
            runtimes_module.forget(hops, _at(now), prediction)
            log.append({"op": "forget", "now": now, "hops": _hops_json(hops)})
    return {
        "policy": {
            "runtime_decay_s": prediction.runtime_decay_s,
            "runtime_prior_weight": prediction.runtime_prior_weight,
            "runtime_ratio_max": prediction.runtime_ratio_max,
            "runtime_keep_decays": prediction.runtime_keep_decays,
        },
        "log": log,
    }


# --- libm -------------------------------------------------------------------------------------


def libm_fixture() -> dict[str, Any]:
    rng = random.Random(3)
    out: dict[str, Any] = {}
    for name, samples in LIBM.items():
        ordered = sorted(samples)
        if len(ordered) > LIBM_SAMPLES:
            ordered = sorted(rng.sample(ordered, LIBM_SAMPLES))
        f = getattr(math, name)
        out[name] = {
            "distinct": len(samples),
            "cases": [[*(bits(a) for a in args), bits(f(*args))] for args in ordered],
        }
    return out


def main() -> None:
    release = Path(sys.argv[1]) if len(sys.argv) > 1 else RELEASE
    policy = load_policy()
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = {
        "path.json": path_fixture(release, policy),
        "runtimes.json": runtimes_fixture(policy),
    }
    fixtures["libm.json"] = libm_fixture()
    for name, document in fixtures.items():
        text = json.dumps(document, separators=(",", ":"), allow_nan=False)
        (OUT / name).write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"{name}: {len(text):,} bytes")
    for name, samples in LIBM.items():
        print(f"libm {name}: {len(samples):,} distinct arguments")


if __name__ == "__main__":
    main()
