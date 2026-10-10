"""Facts as JSON for `rt.observation.facts`, versioned by `FACT_SCHEMA_VERSION`.

Warm replay reads them back, so `facts_from_json(facts_to_json(x)) == x` for every fact type.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast

from obehy.realtime.model import (
    FACT_SCHEMA_VERSION,
    Delay,
    DelayReference,
    EventKind,
    Fact,
    Interval,
    NextPoint,
    NextStop,
    NextStopPrediction,
    PlatformAssignment,
    PlatformLabel,
    PointEvent,
    Position,
    ServiceDay,
    SourceDeparture,
    SourceState,
    StopEvent,
    TripKey,
    TripStatus,
    VehicleKey,
)
from obehy.realtime.times import Instant, instant

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
        case SourceDeparture(at):
            return {"t": "source_departure", "at": at.isoformat()}
        case StopEvent(call_ref, kind, at):
            return {"t": "stop_event", "call": call_ref, "kind": kind, "at": at.isoformat()}
        case NextStop(call_ref):
            return {"t": "next_stop", "call": call_ref}
        case ServiceDay(day):
            return {"t": "service_day", "day": day.isoformat()}
        case PointEvent(name, sr70, scheduled, actual, standing):
            return {
                "t": "point_event",
                "name": name,
                "sr70": sr70,
                "scheduled": _iso(scheduled),
                "lo": actual.lo.isoformat(),
                "hi": actual.hi.isoformat(),
                "standing": standing,
            }
        case NextPoint(name, sr70):
            return {"t": "next_point", "name": name, "sr70": sr70}
        case NextStopPrediction(sr70, scheduled, predicted):
            return {
                "t": "next_stop_prediction",
                "sr70": sr70,
                "scheduled": _iso(scheduled),
                "predicted": _iso(predicted),
            }
        case TripStatus(replacement_bus, diverted):
            return {"t": "trip_status", "replacement_bus": replacement_bus, "diverted": diverted}
        case PlatformAssignment(station, kind, scheduled, value, label):
            return {
                "t": "platform",
                "station": station,
                "kind": kind,
                "scheduled": scheduled.isoformat(),
                "value": value,
                "label": label,
            }


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
        case "source_departure":
            return SourceDeparture(instant(datetime.fromisoformat(str(data["at"]))))
        case "stop_event":
            return StopEvent(
                str(data["call"]),
                cast(EventKind, data["kind"]),
                instant(datetime.fromisoformat(str(data["at"]))),
            )
        case "next_stop":
            return NextStop(str(data["call"]))
        case "service_day":
            return ServiceDay(date.fromisoformat(str(data["day"])))
        case "point_event":
            return PointEvent(
                str(data["name"]),
                _text(data.get("sr70")),
                _instant(data.get("scheduled")),
                Interval(_at(data["lo"]), _at(data["hi"])),
                bool(data["standing"]),
            )
        case "next_point":
            return NextPoint(str(data["name"]), _text(data.get("sr70")))
        case "next_stop_prediction":
            return NextStopPrediction(
                str(data["sr70"]), _instant(data.get("scheduled")), _instant(data.get("predicted"))
            )
        case "trip_status":
            return TripStatus(bool(data["replacement_bus"]), bool(data["diverted"]))
        case "platform":
            kind = str(data["kind"])
            if kind not in ("arrival", "departure"):
                raise FactSchemaError(f"bad platform kind {kind!r}")
            return PlatformAssignment(
                str(data["station"]),
                "arrival" if kind == "arrival" else "departure",
                _at(data["scheduled"]),
                str(data["value"]),
                cast(PlatformLabel, data["label"]),
            )
        case other:
            raise FactSchemaError(f"unknown fact type {other!r}")


def _iso(value: Instant | None) -> str | None:
    return None if value is None else value.isoformat()


def _at(value: object) -> Instant:
    return instant(datetime.fromisoformat(str(value)))


def _instant(value: object) -> Instant | None:
    return None if value is None else _at(value)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def facts_to_json(facts: tuple[Fact, ...]) -> Json:
    return {"v": FACT_SCHEMA_VERSION, "facts": [fact_to_json(fact) for fact in facts]}


def facts_from_json(data: Json) -> tuple[Fact, ...]:
    if data.get("v") != FACT_SCHEMA_VERSION:
        raise FactSchemaError(f"fact schema {data.get('v')!r} is not {FACT_SCHEMA_VERSION}")
    return tuple(fact_from_json(cast(Json, item)) for item in cast(list[Any], data["facts"]))
