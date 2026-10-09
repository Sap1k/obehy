"""Real-trace fixtures: one trip's static slice plus its observations, as JSON.

`extract` (run by hand against the development database and a pinned corpus) writes
`tests/realtime/traces/<name>.json`; `load` rebuilds an `Index` and the observations for unit
tests, which then need neither the database nor the corpus.

    uv run python -m tests.realtime.traces 001521:104 2026-10-06T03:40 2026-10-06T04:45 \\
        --corpus E:/Git/obehy/artifacts/pinned/duk-2026-10-05/archive --name duk-001521-104
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from obehy.realtime.index import Call, Index, KeyEntry, Location, Shape, Trip, parent_ref
from obehy.realtime.model import Feed, Observation, RawRef, TripKey
from obehy.realtime.model_json import facts_from_json, facts_to_json
from obehy.realtime.times import candidate_service_dates, instant

TRACES = Path(__file__).resolve().parent / "traces"
NAMESPACE = "cis:line_trip"


def load(name: str) -> tuple[Index, list[Observation]]:
    data: dict[str, Any] = json.loads((TRACES / f"{name}.json").read_text(encoding="utf-8"))
    index = Index(data["release_id"], cast(Feed, data["feed"]))
    for ref, entries in data["keys"]:
        index.key_entries[(ref[0], ref[1])] = tuple(
            KeyEntry(e[0], date.fromisoformat(e[1]), date.fromisoformat(e[2])) for e in entries
        )
    for t in data["trips"]:
        index.trips[t["trip_id"]] = Trip(
            t["trip_id"],
            t["route_id"],
            t["route_name"],
            t["mode"],
            t["service_id"],
            t["headsign"],
            t["shape_id"],
            tuple(Call(*c) for c in t["calls"]),
        )
    for service, days in data["service_dates"].items():
        index.service_dates[service] = frozenset(date.fromisoformat(d) for d in days)
    index.loaded_dates = frozenset(date.fromisoformat(d) for d in data["loaded_dates"])
    for loc in data["locations"]:
        index.locations[loc[0]] = Location(*loc)
    for shape in data["shapes"]:
        index.shapes[shape["shape_id"]] = Shape(
            shape["shape_id"],
            tuple((p[0], p[1]) for p in shape["points"]),
            tuple(shape["distances_m"]),
        )
    observations = [
        Observation(
            o["source"],
            o["channel"],
            cast(Feed, o["feed"]),
            instant(datetime.fromisoformat(o["received_at"])),
            None if o["observed_at"] is None else instant(datetime.fromisoformat(o["observed_at"])),
            RawRef(o["sha256"], o["item"]),
            o["decoder_version"],
            facts_from_json(o["facts"]),
        )
        for o in data["observations"]
    ]
    return index, observations


def extract(key: str, start: datetime, end: datetime, corpus: Path, name: str) -> Path:
    import psycopg

    from obehy.realtime.archive import iter_polls
    from obehy.realtime.index_sql import IndexLoader, active_loads
    from obehy.realtime.policy import load_policy
    from obehy.realtime.sources import duk
    from obehy.runtime_config import load_database_url

    policy = load_policy()
    observations: list[Observation] = []
    for poll in iter_polls(corpus, "duk", "vehicles", start.date(), end.date()):
        received = instant(poll.received_at)
        body = poll.body()
        if not start <= received < end or body is None or poll.entry.get("error"):
            continue
        for observation in duk.decode(
            body, str(poll.entry["sha256"]), received, policy.time.max_clock_skew
        ):
            trip_key = observation.first(TripKey)
            if trip_key is not None and trip_key.key == key:
                observations.append(observation)
    days = {d for o in observations for d in candidate_service_dates(o.at)}
    with psycopg.connect(load_database_url(), autocommit=True) as connection:
        loader = IndexLoader(connection, active_loads(connection), "jdf")
        index = loader.ensure([(NAMESPACE, key)], days)
    refs = [(NAMESPACE, key)]
    parent = parent_ref((NAMESPACE, key))
    if parent is not None:
        refs.append(parent)
    trips = [index.trip(e.public_id) for e in index.keys(NAMESPACE, key)]
    locations = {c.location_id for t in trips for c in t.calls}
    data = {
        "release_id": index.release_id,
        "feed": index.feed,
        "keys": [
            [
                list(ref),
                [[e.public_id, str(e.valid_from), str(e.valid_to)] for e in index.keys(*ref)],
            ]
            for ref in refs
        ],
        "trips": [
            {
                "trip_id": t.trip_id,
                "route_id": t.route_id,
                "route_name": t.route_name,
                "mode": t.mode,
                "service_id": t.service_id,
                "headsign": t.headsign,
                "shape_id": t.shape_id,
                "calls": [
                    [
                        c.sequence,
                        c.location_id,
                        c.visit_n,
                        c.passenger_service,
                        c.arrival,
                        c.departure,
                    ]
                    for c in t.calls
                ],
            }
            for t in trips
        ],
        "service_dates": {
            t.service_id: sorted(str(d) for d in index.service_dates.get(t.service_id, ()))
            for t in trips
        },
        "loaded_dates": sorted(str(d) for d in index.loaded_dates),
        "locations": [
            [loc.location_id, loc.name, loc.lon, loc.lat]
            for loc in (index.location(i) for i in sorted(locations))
        ],
        "shapes": [
            {
                "shape_id": s.shape_id,
                "points": [list(p) for p in s.points],
                "distances_m": list(s.distances_m),
            }
            for s in (index.shape(t.shape_id) for t in trips if t.shape_id is not None)
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
            for o in observations
        ],
    }
    TRACES.mkdir(exist_ok=True)
    out = TRACES / f"{name}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("key")
    parser.add_argument("start", type=lambda s: instant(datetime.fromisoformat(s + "+00:00")))
    parser.add_argument("end", type=lambda s: instant(datetime.fromisoformat(s + "+00:00")))
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    out = extract(args.key, args.start, args.end, args.corpus, args.name)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
