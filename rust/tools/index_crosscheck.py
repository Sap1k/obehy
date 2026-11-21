"""The Rust index against the Python one (docs/RUST_PORT.md, R3).

    uv run python rust/tools/index_crosscheck.py plan --release DIR --source sz-mapa \
        --channel trains --since 2026-10-10T01:30:00Z --until 2026-10-10T10:00:00Z --out OUT
    uv run python rust/tools/index_crosscheck.py fixtures --out OUT
    uv run python rust/tools/index_crosscheck.py sql --plan OUT/plan.json --expect OUT/expect.json

`plan` decodes an archive window as replay does and turns it into the index's load script:
per batch of polls (`--batch-s`), `ensure` with the keys, lines and service dates the
observations name (the runner's own `_refs`, `_lines`, `observation_days`) and `touch`; the run
trips binding would build; and every `--sweep-every-s` the runner's sweep, keeping keys named in
`--keep-s` and the live journeys (approximated: keys named in the last `--live-s`, their trips
running that day and their runs). It then executes the script afresh with the Python loader on
the release's serving files and writes `expect.json`: a canonical dump of the index at each
`dump` step (`obehy_core::index::dump` writes the same in Rust). The Rust side:

    cargo run --release -p obehy-rt --example index_check -- --plan OUT/plan.json \
        --expect OUT/expect.json

`fixtures` writes the DB parity test's fixture releases with a plan over every key they hold.
`sql` creates a scratch database from the test database URL (`[database] test_url`), migrates
it, loads the plan's release (or, for a real release, a bounded subset with only the rows the
plan's keys reach: `--subset`), runs the Rust check against PostgreSQL, and drops the database.
The URL is passed to the Rust process in the environment, never printed.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import time
import tracemalloc
import uuid
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from obehy.realtime.index import Index, IndexMiss, KeyRef, calls_at_station  # noqa: E402
from obehy.realtime.index_sql import IndexLoader, file_loads  # noqa: E402
from obehy.realtime.times import Instant, instant, local_date  # noqa: E402

Step = dict[str, Any]


# --- the canonical dump (obehy_core::index::dump) ---------------------------------------------


def _f(x: float | None) -> str | None:
    return None if x is None else repr(x)


def dump(index: Index) -> dict[str, Any]:
    loaded = sorted(index.loaded_dates)
    trips = sorted(index.trips.values(), key=lambda t: t.trip_id)
    running: list[list[str]] = []
    for trip_id in sorted(index.trip_services):
        for day in loaded:
            try:
                if index.running_trip(trip_id, day) is not None:
                    running.append([trip_id, day.isoformat()])
            except IndexMiss:
                running.append([trip_id, day.isoformat(), "miss"])
    station_calls: list[list[Any]] = []
    for trip in trips:
        if not trip.parts:
            continue
        codes = sorted(
            {
                code
                for call in trip.calls
                if (code := index.location_key(call.location_id, "sr70")) is not None
            }
        )
        for code in codes:
            station_calls.append(
                [
                    trip.trip_id,
                    code,
                    list(calls_at_station(trip, index, (code,))),
                    list(calls_at_station(trip, index, (code,), start=1, passenger=True)),
                ]
            )
    return {
        "release_id": index.release_id,
        "feed": index.feed,
        "loaded_dates": [d.isoformat() for d in loaded],
        "keys": [
            [ns, i, [[e.public_id, e.valid_from.isoformat(), e.valid_to.isoformat()] for e in es]]
            for (ns, i), es in sorted(index.key_entries.items())
        ],
        "lines": [[ns, line, list(ids)] for (ns, line), ids in sorted(index.lines.items())],
        "trip_services": [[t, s] for t, s in sorted(index.trip_services.items())],
        "service_dates": [
            [s, [d.isoformat() for d in sorted(days)]]
            for s, days in sorted(index.service_dates.items())
        ],
        "runs": [[r, list(parts)] for r, parts in sorted(index.runs.items())],
        "trips": [
            [
                t.trip_id,
                t.route_id,
                t.route_name,
                t.mode,
                t.service_id,
                t.headsign,
                t.shape_id,
                t.run_key,
                t.run_part,
                t.train_number,
                t.agency_id,
                [
                    [
                        c.sequence,
                        c.location_id,
                        c.visit_n,
                        c.passenger_service,
                        c.arrival,
                        c.departure,
                        c.on_request,
                        c.point_id,
                        index.location_key(c.location_id, "sr70"),
                    ]
                    for c in t.calls
                ],
                [[p.trip_id, p.train_number, p.first, p.last] for p in t.parts],
            ]
            for t in trips
        ],
        "locations": [
            [loc.location_id, loc.name, _f(loc.lon), _f(loc.lat)]
            for _, loc in sorted(index.locations.items())
        ],
        "shapes": [
            [
                s.shape_id,
                [
                    [repr(p[0]), repr(p[1]), repr(d)]
                    for p, d in zip(s.points, s.distances_m, strict=True)
                ],
                len(s.distances_m),
            ]
            for _, s in sorted(index.shapes.items())
        ],
        "location_keys": sorted(
            [loc, ns, i] for loc, keys in index.location_keys.items() for ns, i in keys.items()
        ),
        "keyed": [[ns, i, list(locs)] for (ns, i), locs in sorted(index.keyed.items())],
        "running": running,
        "dormant": index.dormant(frozenset(index.loaded_dates)),
        "station_calls": station_calls,
    }


def counts(index: Index) -> dict[str, int]:
    return {
        "keys": len(index.key_entries),
        "absent_keys": sum(1 for e in index.key_entries.values() if not e),
        "trips": len(index.trips),
        "calls": sum(len(t.calls) for t in index.trips.values()),
        "shapes": len(index.shapes),
        "shape_points": sum(len(s.points) for s in index.shapes.values()),
        "dates": len(index.loaded_dates),
        "trip_services": len(index.trip_services),
        "locations": len(index.locations),
        "runs": len(index.runs),
    }


# --- executing a plan -------------------------------------------------------------------------


def _refs(items: Iterable[list[str]]) -> list[KeyRef]:
    return [(ns, i) for ns, i in items]


def _days(items: Iterable[str]) -> list[date]:
    return [date.fromisoformat(d) for d in items]


def _at(text: str) -> Instant:
    return instant(datetime.fromisoformat(text))


def execute(loader: IndexLoader, steps: list[Step]) -> tuple[list[dict[str, Any]], float]:
    """Run the plan's steps; the dumps it asks for and the seconds spent loading."""

    dumps: list[dict[str, Any]] = []
    spent = 0.0
    for step in steps:
        op = step["op"]
        started = time.perf_counter()
        if op == "ensure":
            loader.ensure(_refs(step["refs"]), _days(step["dates"]), _refs(step["lines"]))
        elif op == "touch":
            loader.touch(_refs(step["refs"]), _refs(step["lines"]), _at(step["at"]))
        elif op == "run":
            for trip_id, day in step["trips"]:
                loader.index.run(loader.index.trip(trip_id), date.fromisoformat(day))
        elif op == "sweep":
            loader.sweep(
                _at(step["since"]),
                step["trip_ids"],
                _refs(step["refs"]),
                date.fromisoformat(step["day"]),
            )
        elif op == "dump":
            dumps.append({"name": step["name"], "counts": counts(loader.index)})
            dumps[-1]["index"] = dump(loader.index)
            continue
        else:
            raise ValueError(f"unknown step {op}")
        spent += time.perf_counter() - started
    return dumps, spent


def _warm(loader: IndexLoader) -> IndexLoader:
    """The Arrow tables' first read is the file reader's, not the index's: read them first."""

    rows: Any = loader.rows
    for name in ("_keys", "_trips", "_routes", "_calls", "_locations", "_shape_points"):
        getattr(rows, name)
    rows._calendar, rows._exceptions, rows._services  # noqa: B018
    return loader


def _write_expect(plan: dict[str, Any], out: Path) -> None:
    release = Path(plan["release"])
    loader = _warm(IndexLoader(None, file_loads(release), plan["feed"]))
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    steps = [s for s in plan["steps"] if s["op"] != "dump"]
    started = time.perf_counter()
    _, spent = execute(loader, steps)
    wall = time.perf_counter() - started
    gc.collect()
    heap = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()
    loaded_counts = counts(loader.index)
    del loader
    gc.collect()
    loader = _warm(IndexLoader(None, file_loads(release), plan["feed"]))
    dumps, untraced = execute(loader, plan["steps"])
    document = {
        "plan": plan["name"],
        "python": {
            "load_s": round(untraced, 3),
            "load_s_traced": round(spent, 3),
            "wall_s": round(wall, 3),
            "heap_bytes_after": heap,
            "counts": loaded_counts,
        },
        "dumps": dumps,
    }
    out.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    print(
        f"{plan['name']}: python load {untraced:.2f} s ({spent:.2f} s traced),"
        f" index heap {heap / 1e6:.1f} MB, {loaded_counts}"
    )


# --- making a plan from an archive window -----------------------------------------------------


def _key_list(refs: Iterable[KeyRef]) -> list[list[str]]:
    return [list(r) for r in sorted(set(refs))]


def make_plan(args: argparse.Namespace) -> dict[str, Any]:
    from obehy.realtime.policy import load_policy
    from obehy.realtime.replay import ReplayOptions, decoded_polls, named_channels
    from obehy.realtime.runner import _lines, _refs, observation_days  # pyright: ignore

    policy = load_policy()
    since, until = _at(args.since), _at(args.until)
    options = ReplayOptions(
        release=args.release,
        archive=args.archive,
        start=since.date(),
        end=until.date(),
        channels=named_channels([(args.source, args.channel)]),
        out=args.out,
        feeds=(args.feed,),
        since=since,
        until=until,
        all_feeds=True,
    )
    loader = IndexLoader(None, file_loads(args.release), args.feed)
    index = loader.index
    steps: list[Step] = []
    batch_end = since + timedelta(seconds=args.batch_s)
    next_sweep = since + timedelta(seconds=args.sweep_every_s)
    refs: set[KeyRef] = set()
    lines: set[KeyRef] = set()
    days: set[date] = set()
    last_at = since
    recent: list[tuple[Instant, set[KeyRef], set[str]]] = []
    sweeps = 0

    def flush(at: Instant) -> None:
        nonlocal refs, lines, days
        if not refs and not lines and not days:
            return
        step: Step = {
            "op": "ensure",
            "refs": _key_list(refs),
            "dates": [d.isoformat() for d in sorted(days)],
            "lines": _key_list(lines),
        }
        steps.append(step)
        loader.ensure(refs, days, lines)
        steps.append(
            {"op": "touch", "refs": step["refs"], "lines": step["lines"], "at": at.isoformat()}
        )
        loader.touch(refs, lines, at)
        # What binding does: each key's trips running on each candidate day, joined into runs.
        runs: list[list[str]] = []
        live: set[str] = set()
        for ref in sorted(refs):
            for day in sorted(days):
                for entry in index.keys(*ref):
                    if not entry.valid_on(day):
                        continue
                    trip = index.running_trip(entry.public_id, day)
                    if trip is None:
                        continue
                    live.add(trip.trip_id)
                    if trip.run_key is not None and [trip.trip_id, day.isoformat()] not in runs:
                        runs.append([trip.trip_id, day.isoformat()])
                        live.add(index.run(trip, day).trip_id)
        if runs:
            steps.append({"op": "run", "trips": runs})
        recent.append((at, set(refs), live))
        refs, lines, days = set(), set(), set()

    def sweep(at: Instant) -> None:
        nonlocal sweeps
        horizon = at - timedelta(seconds=args.live_s)
        live_refs = set().union(*(r for t, r, _ in recent if t >= horizon))
        trip_ids = set().union(*(ids for t, _, ids in recent if t >= horizon))
        recent[:] = [item for item in recent if item[0] >= horizon]
        day = local_date(at) - timedelta(days=1)
        if sweeps == 0:
            steps.append({"op": "dump", "name": f"before-sweep-{at.isoformat()}"})
        step: Step = {
            "op": "sweep",
            "since": (at - timedelta(seconds=args.keep_s)).isoformat(),
            "trip_ids": sorted(trip_ids),
            "refs": _key_list(live_refs),
            "day": day.isoformat(),
        }
        steps.append(step)
        loader.sweep(_at(step["since"]), step["trip_ids"], live_refs, day)
        if sweeps == 0:
            steps.append({"op": "dump", "name": f"after-sweep-{at.isoformat()}"})
        sweeps += 1

    for decoded in decoded_polls(options, policy):
        at = decoded.at
        while at >= batch_end:
            flush(last_at)
            if batch_end >= next_sweep:
                sweep(batch_end)
                next_sweep += timedelta(seconds=args.sweep_every_s)
            batch_end += timedelta(seconds=args.batch_s)
        last_at = at
        for observation in decoded.observations or ():
            if observation.feed != args.feed:
                continue
            refs |= _refs(observation)
            lines |= _lines(observation)
            days |= observation_days(observation)
    flush(last_at)
    steps.append({"op": "dump", "name": "end"})
    del policy
    return {
        "name": args.name or f"{args.source}-{args.since}",
        "release": str(args.release.resolve()),
        "feed": args.feed,
        "steps": steps,
    }


def cmd_plan(args: argparse.Namespace) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    plan = make_plan(args)
    print(
        f"plan {plan['name']}: {len(plan['steps'])} steps in {time.perf_counter() - started:.1f} s"
    )
    (args.out / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _write_expect(plan, args.out / "expect.json")


# --- fixture releases -------------------------------------------------------------------------


def _all_keys_plan(name: str, release: Path, feed: str, days: list[date]) -> dict[str, Any]:
    import pyarrow.parquet as pq

    table = pq.read_table(  # pyright: ignore[reportUnknownMemberType]
        release / feed / "serving" / "source_key.parquet", columns=["namespace", "identifier"]
    )
    pairs = sorted(
        set(
            zip(
                table.column("namespace").to_pylist(),
                table.column("identifier").to_pylist(),
                strict=True,
            )
        )
    )
    trip_keys = [p for p in pairs if p[0] not in ("sr70", "sr70:track")]
    lines = sorted(
        {("cis:line", i.split(":", 1)[0]) for ns, i in trip_keys if ns == "cis:line_trip"}
    )
    first, rest = days[:1], days[1:]
    steps: list[Step] = [
        {
            "op": "ensure",
            "refs": [list(p) for p in trip_keys[: len(trip_keys) // 2]],
            "dates": [d.isoformat() for d in first],
            "lines": [],
        },
        {"op": "dump", "name": "half"},
        {
            "op": "ensure",
            "refs": [list(p) for p in trip_keys],
            "dates": [d.isoformat() for d in rest],
            "lines": [list(line) for line in lines],
        },
        {
            "op": "ensure",
            "refs": [["cis:line_trip", "999999:1"]],
            "dates": [],
            "lines": [["cis:line", "999999"]],
        },
        {"op": "dump", "name": "all"},
    ]
    loader = IndexLoader(None, file_loads(release), feed)  # type: ignore[arg-type]
    loader.ensure(_refs(steps[0]["refs"]), first, ())
    loader.ensure(_refs(steps[2]["refs"]), rest, _refs(steps[2]["lines"]))
    runs = [
        [t.trip_id, d.isoformat()]
        for t in sorted(loader.index.trips.values(), key=lambda t: t.trip_id)
        if t.run_key is not None
        for d in days
        if loader.index.running_trip(t.trip_id, d) is not None
    ]
    steps += [
        {"op": "run", "trips": runs},
        {"op": "dump", "name": "runs"},
        {
            "op": "sweep",
            "since": "2026-10-08T00:00:00+00:00",
            "trip_ids": [r for r, _ in runs[:1]],
            "refs": [list(p) for p in trip_keys[:1]],
            "day": days[1].isoformat(),
        },
        {"op": "dump", "name": "swept"},
        {
            "op": "ensure",
            "refs": [list(p) for p in trip_keys],
            "dates": [(days[-1] + timedelta(days=1)).isoformat()],
            "lines": [],
        },
        {"op": "dump", "name": "reloaded"},
    ]
    return {"name": name, "release": str(release), "feed": feed, "steps": steps}


def cmd_fixtures(args: argparse.Namespace) -> None:
    from tests.db.serving_fixture import czptt_rows, jdf_rows, write_release
    from tests.realtime.builder import serving_rows, timetable

    day = date(2026, 10, 8)
    nxt = day + timedelta(days=1)
    tt = (
        timetable()
        .trip("582492:143", days=[day], calls=[("A", "23:50"), ("B", "24:05"), ("A", "24:20")])
        .trip("582492:144", days=[day, nxt], calls=[("B", "06:00"), ("C", "06:20")])
        .trip("582493:1", days=[nxt], calls=[("C", "07:00"), ("D", "07:10")])
        .trip(
            "582492:144",
            days=[day + timedelta(days=5)],
            calls=[("B", "06:00"), ("C", "06:20")],
            trip_id="jdf:t-later",
        )
    )
    root = args.out
    root.mkdir(parents=True, exist_ok=True)
    days = [date(2026, 10, 1) + timedelta(days=n) for n in range(16)]
    for name, release, feeds in (
        (
            "fixture",
            write_release(root, "run-crosscheck", jdf=jdf_rows(), czptt=czptt_rows()),
            ("jdf", "czptt"),
        ),
        ("builder", write_release(root, "run-crosscheck-tt", jdf=serving_rows(tt)), ("jdf",)),
    ):
        for feed in feeds:
            out = root / f"{name}-{feed}"
            out.mkdir(exist_ok=True)
            plan = _all_keys_plan(f"{name}-{feed}", release, feed, days)
            (out / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            _write_expect(plan, out / "expect.json")


# --- the SQL path -----------------------------------------------------------------------------


def _subset_release(plan: dict[str, Any], out: Path) -> Path:
    """The plan's release cut to the rows its keys can reach (bounded, never the whole feed):
    the trips the keys name with every part of their runs, their calls, routes, shapes,
    calendars and locations; all location keys and locations (one per station); the other
    feed is the fixture's."""

    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from tests.db.serving_fixture import czptt_rows, jdf_rows, write_release

    feed = plan["feed"]
    serving = Path(plan["release"]) / feed / "serving"

    def read(table: str) -> Any:
        return pq.read_table(serving / f"{table}.parquet")  # pyright: ignore

    def isin(table: Any, column: str, values: Iterable[Any]) -> Any:
        return table.filter(pc.is_in(table.column(column), value_set=pa.array(sorted(values))))  # pyright: ignore

    import pyarrow as pa

    wanted: set[tuple[str, str]] = set()
    for step in plan["steps"]:
        if step["op"] == "ensure":
            wanted |= {tuple(r) for r in step["refs"]}
            wanted |= {("cis:line", r[1]) for r in step["lines"]}
    keys = read("source_key")
    lines = {i for ns, i in wanted if ns == "cis:line"}
    pairs = list(
        zip(
            keys.column("namespace").to_pylist(), keys.column("identifier").to_pylist(), strict=True
        )
    )
    kinds = keys.column("entity_kind").to_pylist()
    publics = keys.column("public_id").to_pylist()
    named = {
        p
        for (ns, i), p in zip(pairs, publics, strict=True)
        if (ns, i) in wanted or (ns == "cis:line_trip" and i.split(":", 1)[0] in lines)
    }
    trips = read("trip")
    chosen = isin(trips, "trip_id", named & set(trips.column("trip_id").to_pylist()))
    runs = {r for r in chosen.column("run_key").to_pylist() if r is not None}
    if runs:
        chosen = trips.filter(
            pc.or_(  # pyright: ignore
                pc.is_in(trips.column("trip_id"), value_set=chosen.column("trip_id")),  # pyright: ignore
                pc.is_in(trips.column("run_key"), value_set=pa.array(sorted(runs))),  # pyright: ignore
            )
        )
    trip_ids = set(chosen.column("trip_id").to_pylist())
    keep_key = [
        (k == "location")
        or (p in trip_ids)
        or ((ns, i) in wanted)
        or (ns == "cis:line_trip" and i.split(":", 1)[0] in lines)
        for (ns, i), k, p in zip(pairs, kinds, publics, strict=True)
    ]
    rows: dict[str, list[dict[str, Any]]] = {
        "agency": read("agency").to_pylist(),
        "location": read("location").to_pylist(),
        "source_key": keys.filter(pa.array(keep_key)).to_pylist(),
        "trip": chosen.to_pylist(),
    }
    routes = set(chosen.column("route_id").to_pylist())
    route_keys = {
        p
        for (ns, i), k, p in zip(pairs, kinds, publics, strict=True)
        if k == "route" and (ns, i) in wanted
    }
    rows["route"] = isin(read("route"), "route_id", routes | route_keys).to_pylist()
    calls = isin(read("trip_call"), "trip_id", trip_ids).to_pylist()
    for call in calls:
        call["route_stop_id"] = None
    rows["trip_call"] = calls
    shapes = {s for s in chosen.column("shape_id").to_pylist() if s is not None}
    rows["shape"] = isin(read("shape"), "shape_id", shapes).to_pylist() if shapes else []
    rows["shape_point"] = (
        isin(read("shape_point"), "shape_id", shapes).to_pylist() if shapes else []
    )
    services = set(chosen.column("service_id").to_pylist())
    rows["service_calendar"] = isin(read("service_calendar"), "service_id", services).to_pylist()
    rows["service_exception"] = isin(read("service_exception"), "service_id", services).to_pylist()
    for trip in rows["trip"]:
        trip["block_key"] = None
    other = "jdf" if feed == "czptt" else "czptt"
    fixture = {"jdf": jdf_rows, "czptt": czptt_rows}[other]()
    run_id = f"subset-{uuid.uuid4().hex[:8]}"
    release = write_release(out, run_id, **{feed: rows, other: fixture})
    print(
        f"subset release: {len(rows['trip'])} trips, {len(rows['trip_call'])} calls,"
        f" {len(rows['shape_point'])} shape points"
    )
    return release


def cmd_sql(args: argparse.Namespace) -> None:
    import psycopg
    from psycopg import conninfo, sql

    from obehy.release.contract import load_contract
    from obehy.release.load import load_release
    from obehy.release.migrate import migrate
    from obehy.runtime_config import load_test_database_url

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    base = load_test_database_url()
    if not base:
        raise SystemExit("no test database: [database] test_url in config/obehy.local.toml")
    work = args.plan.parent
    release = Path(plan["release"])
    if args.subset:
        release = _subset_release(plan, work / "subset")
        sub_plan = dict(plan, release=str(release))
        (work / "plan-subset.json").write_text(json.dumps(sub_plan), encoding="utf-8")
        # The subset must index exactly as the whole release: check it in Python first.
        _write_expect(sub_plan, work / "expect-subset.json")
        whole = json.loads(args.expect.read_text(encoding="utf-8"))["dumps"]
        part = json.loads((work / "expect-subset.json").read_text(encoding="utf-8"))["dumps"]
        for a, b in zip(whole, part, strict=True):
            b["index"]["release_id"] = a["index"]["release_id"]
            assert a == b, f"the subset release indexes differently at {a['name']}"
        print("subset release indexes like the whole one (Python)")
    name = f"obehy_r3_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = conninfo.make_conninfo(base, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            migrate(connection)
            started = time.perf_counter()
            load_release(connection, release, load_contract(), report=lambda _: None)
            print(
                f"release loaded into a scratch database in {time.perf_counter() - started:.1f} s"
            )
            run_id = json.loads((release / "release.json").read_text(encoding="utf-8"))["run_id"]
            row = connection.execute(
                "SELECT max(load_id) FROM control.load WHERE run_id = %s AND package = %s",
                (run_id, plan["feed"]),
            ).fetchone()
            assert row is not None and row[0] is not None
            load_id = int(row[0])
            # The reference for the Rust SQL rows is Python over the same database: the
            # database's collation orders text, where the files' rows order by code point.
            from obehy.realtime.index_sql import run_loads

            loads = run_loads(connection, run_id)
            assert loads is not None
            loader = IndexLoader(connection, loads, plan["feed"])
            dumps, spent = execute(loader, plan["steps"])
            reference = json.loads(args.expect.read_text(encoding="utf-8"))
            for mine, theirs in zip(dumps, reference["dumps"], strict=True):
                mine["index"]["release_id"] = theirs["index"]["release_id"]
                differ = [k for k in mine["index"] if mine["index"][k] != theirs["index"][k]]
                if differ:
                    print(f"python over SQL differs from files at {mine['name']}: {differ}")
            print(f"python over SQL: {spent:.2f} s loading")
            expect_sql = args.plan.parent / "expect-sql.json"
            reference["python"]["load_s"] = round(spent, 3)
            reference["dumps"] = dumps
            expect_sql.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
            loads_by_feed = dict(
                connection.execute(
                    "SELECT package, max(load_id) FROM control.load WHERE run_id = %s"
                    " GROUP BY package",
                    (run_id,),
                ).fetchall()
            )
        environment = dict(os.environ, OBEHY_INDEX_CHECK_URL=url)
        if args.parity:
            for feed in ("jdf", "czptt"):
                parity = [
                    *("cargo", "run", "--release", "-q", "-p", "obehy-rt"),
                    *("--example", "index_parity", "--", "--release", str(release)),
                    *("--feed", feed, "--sql-env", "OBEHY_INDEX_CHECK_URL"),
                    *("--load-id", str(loads_by_feed[feed])),
                ]
                print(f"parity of the rows, {feed}:")
                done = subprocess.run(parity, cwd=args.rust_dir, env=environment, check=False)
                if done.returncode != 0:
                    raise SystemExit(done.returncode)
        command = [
            "cargo",
            "run",
            "--release",
            "-q",
            "-p",
            "obehy-rt",
            "--example",
            "index_check",
            "--",
            "--plan",
            str(args.plan),
            "--expect",
            str(expect_sql),
            "--sql-env",
            "OBEHY_INDEX_CHECK_URL",
            "--load-id",
            str(load_id),
            "--release-id",
            json.loads(args.expect.read_text(encoding="utf-8"))["dumps"][0]["index"]["release_id"],
        ]
        result = subprocess.run(command, cwd=args.rust_dir, env=environment, check=False)
        if result.returncode != 0:
            raise SystemExit(result.returncode)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--release", type=Path, required=True)
    plan.add_argument("--archive", type=Path, default=REPO / "data" / "rt-raw-prod")
    plan.add_argument("--source", required=True)
    plan.add_argument("--channel", required=True)
    plan.add_argument("--feed", default="czptt")
    plan.add_argument("--since", required=True)
    plan.add_argument("--until", required=True)
    plan.add_argument("--batch-s", type=int, default=300)
    plan.add_argument("--sweep-every-s", type=int, default=3600)
    plan.add_argument("--keep-s", type=int, default=14400)
    plan.add_argument("--live-s", type=int, default=1800)
    plan.add_argument("--name")
    plan.add_argument("--out", type=Path, required=True)
    fixtures = sub.add_parser("fixtures")
    fixtures.add_argument("--out", type=Path, required=True)
    on_sql = sub.add_parser("sql")
    on_sql.add_argument("--plan", type=Path, required=True)
    on_sql.add_argument("--expect", type=Path, required=True)
    on_sql.add_argument("--subset", action="store_true")
    on_sql.add_argument("--parity", action="store_true", help="compare every Rows method")
    on_sql.add_argument("--rust-dir", type=Path, default=REPO / "rust")
    args = parser.parse_args()
    {"plan": cmd_plan, "fixtures": cmd_fixtures, "sql": cmd_sql}[args.command](args)


if __name__ == "__main__":
    main()
