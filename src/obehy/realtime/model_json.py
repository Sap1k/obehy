"""Facts as JSON for `rt.observation.facts`, versioned by `FACT_SCHEMA_VERSION`.

Warm replay reads them back, so `facts_from_json(facts_to_json(x)) == x` for every fact type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from obehy.realtime.model import (
    FACT_SCHEMA_VERSION,
    Delay,
    DelayReference,
    EventKind,
    Fact,
    NextStop,
    Position,
    SourceState,
    StopEvent,
    TripKey,
    VehicleKey,
)
from obehy.realtime.times import instant

Json = dict[str, Any]


class FactSchemaError(ValueError):
    """Stored facts that this code cannot read (another schema version or a bad shape)."""


def fact_to_json(fact: Fact) -> Json:
    match fact:
        case VehicleKey(source_vehicle_id):
            return {"t": "vehicle_key", "id": source_vehicle_id}
        case TripKey(namespace, key):
            return {"t": "trip_key", "ns": namespace, "key": key}
        case Position(lat, lon, bearing):
            return {"t": "position", "lat": lat, "lon": lon, "bearing": bearing}
        case Delay(seconds, reference):
            return {"t": "delay", "s": seconds, "ref": reference}
        case SourceState(code):
            return {"t": "source_state", "code": code}
        case StopEvent(call_ref, kind, at):
            return {"t": "stop_event", "call": call_ref, "kind": kind, "at": at.isoformat()}
        case NextStop(call_ref):
            return {"t": "next_stop", "call": call_ref}


def fact_from_json(data: Json) -> Fact:
    match data.get("t"):
        case "vehicle_key":
            return VehicleKey(str(data["id"]))
        case "trip_key":
            return TripKey(str(data["ns"]), str(data["key"]))
        case "position":
            bearing = data.get("bearing")
            return Position(
                float(data["lat"]), float(data["lon"]), None if bearing is None else float(bearing)
            )
        case "delay":
            return Delay(int(data["s"]), cast(DelayReference, data["ref"]))
        case "source_state":
            return SourceState(str(data["code"]))
        case "stop_event":
            return StopEvent(
                str(data["call"]),
                cast(EventKind, data["kind"]),
                instant(datetime.fromisoformat(str(data["at"]))),
            )
        case "next_stop":
            return NextStop(str(data["call"]))
        case other:
            raise FactSchemaError(f"unknown fact type {other!r}")


def facts_to_json(facts: tuple[Fact, ...]) -> Json:
    return {"v": FACT_SCHEMA_VERSION, "facts": [fact_to_json(fact) for fact in facts]}


def facts_from_json(data: Json) -> tuple[Fact, ...]:
    if data.get("v") != FACT_SCHEMA_VERSION:
        raise FactSchemaError(f"fact schema {data.get('v')!r} is not {FACT_SCHEMA_VERSION}")
    return tuple(fact_from_json(cast(Json, item)) for item in cast(list[Any], data["facts"]))
