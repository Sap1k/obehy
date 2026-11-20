"""A corpus of real journeys as fixtures, and a quick benchmark of the core over them.

About a hundred journeys from bounded windows of the local raw archives, across sources and
modes: city and regional buses, trams, trolleybuses, metro and ferries (PID), regional buses and
trains (DÚK, MPVnet), a city's buses (DPMD), long-distance buses (Arriva Express, keyless) and
trains on the SŽ map. Each is one journey's observations and its static slice, read from a
fetched release's serving files (no database), as `corpus/<name>.json.gz`.

    uv run python -m tests.realtime.corpus build --release-dir E:/Git/obehy/artifacts/fetched/... \\
        --archive E:/Git/obehy/rt-data/rt-raw --extra E:/Git/obehy/work/rt-extra \\
        --examples E:/Git/obehy/work/replay-baseline-pid/report.json
    uv run python -m tests.realtime.corpus bench --out report.json

`build` decodes each window once and loads each feed's index once. Selection is
deterministic: per source and stratum, journeys with enough fixes in a stable hash order, plus
journeys a replay's quality checks named (`--examples`). `bench` steps every journey through
the core with the replay's emit ticks and reports `quality.QualityLog` over all of them.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from obehy.realtime.core import Context, estimate_all, refresh, step
from obehy.realtime.index import CHILD_NAMESPACE, Index, KeyRef, Trip, parent_ref
from obehy.realtime.model import (
    Effect,
    FeedState,
    LineRef,
    Observation,
    Position,
    TripKey,
    VehicleKey,
    WriteEvent,
)
from obehy.realtime.model_json import facts_to_json
from obehy.realtime.policy import load_policy
from obehy.realtime.quality import QualityLog
from obehy.realtime.times import Instant, candidate_service_dates, instant
from tests.realtime.traces import from_json

CORPUS = Path(__file__).resolve().parent / "corpus"
POLICY = load_policy()
TICK = timedelta(seconds=POLICY.emit_tick_s)
AFTER = timedelta(minutes=35)  # ticks after the last observation: stale, lost, finished
MIN_FIXES = 15
MIN_SPAN = timedelta(minutes=15)


def _utc(text: str) -> Instant:
    return instant(datetime.fromisoformat(text).replace(tzinfo=UTC))


@dataclass(frozen=True, slots=True)
class Window:
    """One source's polls in a bounded window, and how many journeys to take per stratum."""

    source: str
    channels: tuple[str, ...]
    extra: bool  # in the unpacked tarballs (`--extra`), not the main archive
    start: Instant
    end: Instant
    quotas: dict[str, int]


# Local times: PID 06:45-09:00 on 10-15, DÚK 06:00-09:00 on 10-06, DPMD and MPVnet 06:00-08:30
# on 10-12, the SŽ map 06:00-09:00 on 10-12, Arriva Express 07:00-13:00 on 10-12 (UTC + 2 h).
WINDOWS = (
    Window(
        "pid",
        ("vehicles",),
        False,
        _utc("2026-10-15T04:45"),
        _utc("2026-10-15T07:00"),
        {
            "bus-city": 16,
            "bus-regional": 14,
            "tram": 10,
            "trolleybus": 3,
            "metro": 3,
            "rail": 3,
            "water": 1,
        },
    ),
    Window(
        "duk",
        ("vehicles",),
        False,
        _utc("2026-10-06T04:00"),
        _utc("2026-10-06T07:00"),
        {"bus-regional": 10, "rail": 5},
    ),
    Window(
        "dpmd",
        ("vehicles",),
        True,
        _utc("2026-10-12T04:00"),
        _utc("2026-10-12T06:30"),
        {"bus-city": 6, "trolleybus": 2},
    ),
    Window(
        "mpvnet",
        ("idol", "jikord", "odis", "zlin"),
        True,
        _utc("2026-10-12T04:00"),
        _utc("2026-10-12T06:30"),
        {"bus-city": 3, "bus-regional": 9, "rail": 4},
    ),
    Window(
        "sz-mapa",
        ("trains",),
        False,
        _utc("2026-10-12T04:00"),
        _utc("2026-10-12T07:00"),
        {"rail": 4, "rail-long": 5},
    ),
    Window(
        "arriva-express",
        ("buses",),
        True,
        _utc("2026-10-12T05:00"),
        _utc("2026-10-12T11:00"),
        {"bus-long": 4},
    ),
)


@dataclass(slots=True)
class Group:
    """One journey's observations from one source: a trip key, or a keyless vehicle on a line."""

    source: str
    feed: str
    key: KeyRef  # the first trip key; keyless: ("cis:line", line)
    vehicle: str
    observations: list[Observation] = field(default_factory=list[Observation])

    @property
    def keyless(self) -> bool:
        return self.key[0] == "cis:line"

    def fixes(self) -> list[Instant]:
        return [o.at for o in self.observations if o.first(Position) is not None]

    def refs(self) -> list[KeyRef]:
        found = {(k.namespace, k.key) for o in self.observations for k in o.all(TripKey)}
        for ref in list(found):
            parent = parent_ref(ref)
            if parent is not None:
                found.add(parent)
        return sorted(found)


def _decoded(window: Window, archive: Path) -> Iterator[Observation]:
    from obehy.realtime.replay import DECODERS, merged_polls

    channels = [(window.source, channel) for channel in window.channels]
    skew = POLICY.time.max_clock_skew
    for received, source, channel, poll in merged_polls(
        archive, channels, window.start.date(), window.end.date()
    ):
        at = instant(received)
        if not window.start <= at < window.end:
            continue
        body = poll.body()
        status = poll.entry.get("status")
        if body is None or poll.entry.get("error") or not isinstance(status, int) or status >= 300:
            continue
        request = poll.entry.get("request")
        try:
            yield from DECODERS[(source, channel)](
                body,
                str(poll.entry["sha256"]),
                at,
                skew,
                cast(dict[str, Any], request) if isinstance(request, dict) else {},
            )
        except ValueError:
            continue


def groups_of(window: Window, archive: Path) -> list[Group]:
    """The window's journeys: observations by source vehicle and first trip key (keyless: by
    vehicle and line)."""

    found: dict[tuple[str, KeyRef], Group] = {}
    for observation in _decoded(window, archive):
        vehicle, key = observation.first(VehicleKey), observation.first(TripKey)
        if vehicle is None:
            continue
        if key is not None:
            ref: KeyRef = (key.namespace, key.key)
        else:
            line = observation.first(LineRef)
            if line is None:
                continue
            ref = (line.namespace, line.value)
        group = found.setdefault(
            (vehicle.source_vehicle_id, ref),
            Group(window.source, observation.feed, ref, vehicle.source_vehicle_id),
        )
        group.observations.append(observation)
    return [found[k] for k in sorted(found)]


def _long_enough(group: Group) -> bool:
    fixes = group.fixes()
    return len(fixes) >= MIN_FIXES and max(fixes) - min(fixes) >= MIN_SPAN


def _stable(group: Group) -> str:
    return hashlib.sha1(f"{group.source}|{group.key}|{group.vehicle}".encode()).hexdigest()


def _running(index: Index, group: Group) -> tuple[Trip, date] | None:
    """The trip a keyed group runs and its service date, as the core would bind it."""

    first = group.observations[0].at
    for day in candidate_service_dates(first):
        for ref in group.refs():
            for entry in index.key_entries.get(ref, ()):
                trip = index.trips.get(entry.public_id)
                if trip is None or not entry.valid_on(day) or len(trip.calls) < 3:
                    continue
                if day in index.loaded_dates and index.runs_on(trip.service_id, day):
                    return trip, day
    return None


def _stratum(source: str, trip: Trip | None, group: Group) -> str:
    if group.keyless:
        return "bus-long"
    if trip is None:
        return "unknown"
    mode = "rail" if group.feed == "czptt" else trip.mode
    if mode == "rail" and re.match(r"(?i)(Ex|R|Rx|RJ|IC|EC|EN|SC|LE|D)(\d|\s|$)", trip.route_name):
        return "rail-long"
    if mode != "bus":
        return mode
    if source in ("dpmd",):
        return "bus-city"
    number = re.match(r"\d+", trip.route_name)
    if source == "pid":
        return "bus-city" if number and int(number.group()) < 300 else "bus-regional"
    if source == "mpvnet":
        # Regional lines have three digits or more; a city's have one or two.
        return "bus-city" if number and int(number.group()) < 100 else "bus-regional"
    return "bus-regional"


def _name(group: Group, stratum: str) -> str:
    key = re.sub(r"[^0-9A-Za-z]+", "-", group.key[1]).strip("-")[:40]
    vehicle = re.sub(r"[^0-9A-Za-z]+", "-", group.vehicle).strip("-")[:16]
    return f"{group.source}-{stratum}-{key}-{vehicle}"


def _serialize(index: Index, group: Group, trips: list[Trip], days: set[date]) -> dict[str, Any]:
    refs = group.refs()
    lines: list[list[Any]] = []
    if group.keyless:
        kept = list(index.lines.get(group.key, ()))
        lines = [[list(group.key), kept]]
        refs = sorted({group.key, *((CHILD_NAMESPACE[group.key[0]], k) for k in kept)})
    runs = sorted({t.run_key for t in trips if t.run_key is not None and t.run_key in index.runs})
    with_parts = {t.trip_id: t for t in trips}
    for run in runs:
        for part in index.runs[run]:
            if part in index.trips:
                with_parts[part] = index.trips[part]
    ordered = [with_parts[t] for t in sorted(with_parts)]
    places = {p for t in ordered for c in t.calls for p in (c.location_id, c.placed_at)}
    codes = {
        index.location_keys[p]["sr70"] for p in places if "sr70" in index.location_keys.get(p, {})
    }
    location_keys: list[list[str]] = []
    for (namespace, identifier), located in sorted(index.keyed.items()):
        if (namespace == "sr70" and identifier in codes) or (
            namespace == "sr70:track" and identifier.split(":", 1)[0] in codes
        ):
            for location_id in located:
                location_keys.append([location_id, namespace, identifier])
                places.add(location_id)
    shapes = sorted({t.shape_id for t in ordered if t.shape_id and t.shape_id in index.shapes})
    return {
        "release_id": index.release_id,
        "feed": index.feed,
        "keys": [
            [list(ref), [[e.public_id, str(e.valid_from), str(e.valid_to)] for e in entries]]
            for ref in refs
            if (entries := index.key_entries.get(ref)) is not None
        ],
        "lines": lines,
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
        "runs": [[run, list(index.runs[run])] for run in runs],
        "location_keys": location_keys,
        "service_dates": {
            t.service_id: sorted(
                str(d) for d in index.service_dates.get(t.service_id, frozenset[date]()) & days
            )
            for t in ordered
        },
        "loaded_dates": sorted(str(d) for d in days),
        "locations": [
            [loc.location_id, loc.name, loc.lon, loc.lat]
            for loc in (index.locations[p] for p in sorted(places) if p in index.locations)
        ],
        "shapes": [
            {
                "shape_id": s.shape_id,
                "points": [list(p) for p in s.points],
                "distances_m": list(s.distances_m),
            }
            for s in (index.shapes[i] for i in shapes)
        ],
        "observations": [
            {
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
            for o in group.observations
        ],
    }


def _example_keys(report: Path | None) -> set[str]:
    """Journey keys a replay's quality checks gave as examples, two per check."""

    if report is None:
        return set()
    examples = json.loads(report.read_text(encoding="utf-8"))["quality"]["checks"]["examples"]
    return {e["key"] for found in examples.values() for e in found[:2]}


def build(release_dir: Path, archive: Path, extra: Path, examples: Path | None) -> list[str]:
    from obehy.realtime.index_sql import IndexLoader, file_loads

    started = time.perf_counter()
    loads = file_loads(release_dir)
    loaders = {feed: IndexLoader(None, loads, feed) for feed in ("jdf", "czptt")}  # type: ignore[arg-type]
    wanted = _example_keys(examples)
    CORPUS.mkdir(exist_ok=True)
    for old in CORPUS.glob("*.json.gz"):
        old.unlink()
    manifest: list[dict[str, Any]] = []
    for window in WINDOWS:
        root = extra if window.extra else archive
        tick = time.perf_counter()
        groups = [g for g in groups_of(window, root) if _long_enough(g)]
        forced = [g for g in groups if any(r[1] in wanted for r in g.refs())]
        chosen = {id(g): g for g in groups}
        days = {d for g in chosen.values() for d in candidate_service_dates(g.observations[0].at)}
        for feed in sorted({g.feed for g in chosen.values()}):
            keyed = sorted({r for g in chosen.values() if g.feed == feed for r in g.refs()})
            lines = sorted({g.key for g in chosen.values() if g.feed == feed and g.keyless})
            loaders[feed].ensure(keyed, days, lines)
        taken: Counter[str] = Counter()
        kept = 0
        for group in [*forced, *sorted(groups, key=_stable)]:
            index = loaders[group.feed].index
            if group.keyless:
                from tests.realtime.traces import _near  # pyright: ignore[reportPrivateUsage]

                first, last = group.observations[0].at, group.observations[-1].at
                near, _ = _near(index, group.key[1], first, last)
                trip = near[0] if near else None
                day = first.date()
                # Inference weighs every trip of the line, not only those near the window.
                child = CHILD_NAMESPACE[group.key[0]]
                trips = [
                    index.trips[e.public_id]
                    for k in index.lines.get(group.key, ())
                    for e in index.key_entries.get((child, k), ())
                    if e.public_id in index.trips
                ]
            else:
                found = _running(index, group)
                if found is None:
                    continue
                trip, day = found
                trips = list(
                    {
                        e.public_id: index.trips[e.public_id]
                        for r in group.refs()
                        for e in index.key_entries.get(r, ())
                        if e.public_id in index.trips
                    }.values()
                )
            if trip is None:
                continue
            stratum = _stratum(window.source, trip, group)
            is_forced = group in forced
            if not is_forced and taken[stratum] >= window.quotas.get(stratum, 0):
                continue
            name = _name(group, stratum)
            if (CORPUS / f"{name}.json.gz").exists():
                continue
            taken[stratum] += 0 if is_forced else 1
            kept += 1
            data = _serialize(
                index, group, trips, set(candidate_service_dates(group.observations[0].at))
            )
            with gzip.open(CORPUS / f"{name}.json.gz", "wt", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False, separators=(",", ":"))
            manifest.append(
                {
                    "name": name,
                    "source": window.source,
                    "stratum": stratum,
                    "key": list(group.key),
                    "vehicle": group.vehicle,
                    "service_date": str(day),
                    "route": trip.route_name,
                    "observations": len(group.observations),
                    "why": "check example" if is_forced else "sample",
                }
            )
        print(
            f"{window.source}: {len(groups)} journeys, kept {kept} {dict(sorted(taken.items()))}"
            f" in {time.perf_counter() - tick:.0f} s"
        )
    manifest.sort(key=lambda m: m["name"])
    (CORPUS / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"{len(manifest)} journeys in {time.perf_counter() - started:.0f} s")
    return [m["name"] for m in manifest]


def names() -> list[str]:
    return sorted(p.name.removesuffix(".json.gz") for p in CORPUS.glob("*.json.gz"))


def load(name: str) -> tuple[Index, list[Observation]]:
    with gzip.open(CORPUS / f"{name}.json.gz", "rt", encoding="utf-8") as data:
        return from_json(json.load(data))


@dataclass(slots=True)
class Run:
    """One journey stepped through the core with the replay's emit ticks."""

    state: FeedState
    effects: list[Effect]

    @property
    def events(self) -> list[WriteEvent]:
        return [e for e in self.effects if isinstance(e, WriteEvent)]


def run(
    name: str,
    quality: QualityLog | None = None,
    tick: Callable[[FeedState, Instant], None] | None = None,
) -> Run:
    """Step `name` through the core; `quality` and `tick` see the state after every tick."""

    from obehy.realtime.runner import shipped_semantics

    index, observations = load(name)
    ctx = Context(index, POLICY, semantics=shipped_semantics())
    state = FeedState(index.feed)
    effects: list[Effect] = []
    ordered = sorted(observations, key=lambda o: (o.received_at, o.source, o.raw.item))
    first = ordered[0].received_at
    now = Instant(first - timedelta(seconds=first.timestamp() % TICK.total_seconds()) + TICK)
    end = ordered[-1].received_at + AFTER
    pending = iter(ordered)
    upcoming = next(pending, None)
    while now <= end:
        while upcoming is not None and upcoming.received_at <= now:
            effects.extend(step(state, upcoming, ctx))
            upcoming = next(pending, None)
        effects.extend(refresh(state, now, ctx))
        estimate_all(state, ctx, now)
        if quality is not None:
            quality.sample(state, ctx, now)
        if tick is not None:
            tick(state, now)
        now = Instant(now + TICK)
    return Run(state, effects)


def bench() -> dict[str, Any]:
    started = time.perf_counter()
    quality = QualityLog()
    for name in names():
        run(name, quality)
    report = quality.report()
    report["journeys"] = len(names())
    report["seconds"] = round(time.perf_counter() - started, 1)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--release-dir", type=Path, required=True)
    builder.add_argument("--archive", type=Path, required=True)
    builder.add_argument("--extra", type=Path, required=True)
    builder.add_argument("--examples", type=Path)
    bencher = commands.add_parser("bench")
    bencher.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args.release_dir, args.archive, args.extra, args.examples)
        return
    report = bench()
    text = json.dumps(report, indent=1, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps({"journeys": report["journeys"], "seconds": report["seconds"]}))


if __name__ == "__main__":
    main()
