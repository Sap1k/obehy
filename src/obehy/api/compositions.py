"""Train compositions (BASE_PLAN.md sections 18.2 and 28): asked lazily, answered by the worker's
lookup broker. The API never calls upstream: a missing or older answer is recorded as a lookup
request (`rt.lookup_request`, NOTIFY `obehy_lookup`) and answered `pending` (202) or `stale`
until the broker has answered it.

A journey's compositions from every source are listed as units, each with its span over the
trip's calls, and as segments: the trip split at every span boundary, each piece with the
units running it (SQL, `rt.composition_spans`).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from obehy.api import schema
from obehy.api.codes import load_codes
from obehy.api.common import LimitsDep, SnapshotDep, envelope, realtime_cache
from obehy.api.db import request_lookup
from obehy.realtime.compositions import JOURNEY_NAMESPACES
from obehy.reference.vocabulary import load_vocabulary

CompositionStatus = Literal["fresh", "stale", "pending", "not_available"]
router = APIRouter(tags=["trips"])
KIND = "composition"
# By (answered lately, has units).
STATUS: dict[tuple[bool, bool], CompositionStatus] = {
    (True, True): "fresh",
    (True, False): "not_available",
    (False, True): "stale",
    (False, False): "pending",
}


def _feature(feature: str, texts: dict[str, str]) -> schema.UnitFeature:
    attribute = load_vocabulary().attributes.get(feature)
    code = feature.rsplit(":", 1)[-1] if feature.startswith("raw:") else None
    text = texts.get(code) if code is not None else None
    if attribute is None:
        return schema.UnitFeature(feature=feature, label=None, icon=None, text=text or None)
    label = schema.Label(cs=attribute.cs, en=attribute.en)
    return schema.UnitFeature(feature=feature, label=label, icon=attribute.icon, text=None)


def _sources(outcomes: dict[str, Any]) -> list[schema.CompositionSource]:
    codes = load_codes()
    return [
        schema.CompositionSource(
            lookup=lookup,
            source=lookup.split("/", 1)[0],
            label=codes.source(lookup.split("/", 1)[0]),
            outcome=answer.get("outcome"),
            reason=answer.get("reason"),
        )
        for lookup, answer in sorted(outcomes.items())
    ]


@router.get(
    "/trips/{trip_id}/{service_date}/composition",
    response_model=schema.Composition,
    responses={
        202: {"model": schema.Composition, "description": "Asked; retry after `retry_after_s`"},
        404: {"model": schema.Problem},
    },
    summary="A trip's composition from a call onward: units with their spans, and segments",
)
def composition(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    trip_id: str,
    service_date: date,
    from_location: Annotated[
        str | None,
        Query(alias="from", description="A call's location_id; default: the next passenger call"),
    ] = None,
) -> schema.Composition:
    trip = snap.one(
        "composition_trip",
        {
            "trip_id": trip_id,
            "service_date": service_date,
            "from": from_location,
            "namespaces": list(JOURNEY_NAMESPACES),
        },
    )
    if trip is None:
        raise HTTPException(404, f"no trip {trip_id}")
    if not trip["runs"]:
        raise HTTPException(404, f"{trip_id} does not run on {service_date}")
    if not trip["from_on_trip"]:
        raise HTTPException(404, f"{from_location} is not a call of {trip_id}")
    journey = {
        "trip_id": trip_id,
        "feed": trip["feed"],
        "key_namespace": trip["key_namespace"],
        "key": trip["key"],
        "service_date": service_date,
    }
    asked_params = {
        "kind": KIND,
        "feed": trip["feed"],
        "trip_id": trip_id,
        "service_date": service_date,
        "from_location": from_location or "",
    }
    asked = snap.one("composition_request", asked_params)
    fresh = (
        asked is not None
        and not asked["pending"]
        and asked["answered_at"] >= snap.now - timedelta(seconds=limits.composition_fresh_s)
    )
    if not fresh and (asked is None or not asked["pending"]):
        request_lookup(cast(ConnectionPool[Any], request.app.state.requests), asked_params)
    calls = {
        r["sequence"]: schema.CompositionCall.model_validate(r)
        for r in snap.all("composition_calls", journey)
    }
    rows = snap.all("composition_units", journey)
    codes = load_codes()
    units = [
        schema.CompositionUnit(
            source=r["source"],
            label=codes.source(r["source"]),
            sequence=r["unit_sequence"],
            kind=r["kind"],
            role=r["role"],
            vehicle_id=r["vehicle_id"],
            vehicle_type=r["vehicle_type"],
            coach_number=r["coach_number"],
            classes=r["classes"],
            features=[_feature(f, r["texts"]) for f in r["features"]],
            destination=r["destination"],
            details=r["details"],
            from_call=calls[r["from_sequence"]],
            to_call=calls[r["to_sequence"]],
            open=r["open"],
            observed_at=r["observed_at"],
        )
        for r in rows
    ]
    position = {(r["composition_id"], r["unit_sequence"]): n for n, r in enumerate(rows)}
    segments = [
        schema.CompositionSegment(
            from_call=calls[s["from_sequence"]],
            to_call=calls[s["to_sequence"]],
            units=[position[(composition_id, sequence)] for composition_id, sequence in s["units"]],
        )
        for s in snap.all("composition_segments", journey)
    ]
    status = STATUS[(fresh, bool(units))]
    retry_after = None
    if status == "pending":
        retry_after = limits.composition_retry_after_s
        response.status_code = 202
        response.headers["Retry-After"] = str(retry_after)
        response.headers["Cache-Control"] = "no-store"
    else:
        realtime_cache(response, limits)
    return schema.Composition(
        **envelope(snap),
        trip_id=trip_id,
        service_date=service_date,
        from_location_id=from_location,
        status=status,
        retry_after_s=retry_after,
        requested_at=None if asked is None else asked["requested_at"],
        answered_at=None if asked is None else asked["answered_at"],
        sources=_sources(asked["outcomes"] if asked is not None else {}),
        units=units,
        segments=segments,
    )
