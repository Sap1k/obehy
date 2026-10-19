"""Trips by their feed trip ID and service date; source keys only as lookup aliases.

A trip ID lasts one timetable version of its line (BASE_PLAN.md section 6). One from an older
version answers 410 with the journey it ran as (from history), whose key `by-key` resolves
against the active release.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, realtime_cache

router = APIRouter(tags=["trips"])

# The journey key a trip is shown with, by preference: the national CIS key, PID's, the train.
JOURNEY_NAMESPACES = ["cis:line_trip", "pid:gtfs_trip_id", "czptt:train_number"]


def trip_path(trip_id: str, service_date: date) -> str:
    return f"/api/v1/trips/{quote(trip_id, safe=':')}/{service_date.isoformat()}"


def by_key_path(namespace: str, key: str, service_date: date) -> str:
    return (
        f"/api/v1/trips/by-key/{quote(namespace, safe=':')}/{quote(key, safe=':')}/"
        f"{service_date.isoformat()}"
    )


@router.get(
    "/trips/by-key/{namespace}/{key}/{service_date}",
    status_code=308,
    response_class=RedirectResponse,
    responses={404: {"model": schema.Problem}, 409: {"model": schema.Problem}},
    summary="Find the trip a source key names on a date (308 to the trip)",
)
def trip_by_key(
    snap: SnapshotDep, namespace: str, key: str, service_date: date
) -> RedirectResponse:
    rows = snap.all(
        "trip_by_key", {"namespace": namespace, "key": key, "service_date": service_date}
    )
    if not rows:
        raise HTTPException(404, f"no trip {namespace} {key} runs on {service_date}")
    runs = {r["run_key"] for r in rows}
    if len(rows) > 1 and (len(runs) > 1 or None in runs):
        names = ", ".join(r["trip_id"] for r in rows)
        raise HTTPException(
            409, f"{namespace} {key} names several trips on {service_date}: {names}"
        )
    return RedirectResponse(trip_path(rows[0]["trip_id"], service_date), status_code=308)


@router.get(
    "/trips/{trip_id}/{service_date}",
    response_model=schema.Trip,
    responses={404: {"model": schema.Problem}, 410: {"model": schema.GoneTrip}},
    summary="A trip on a service date: calls, realtime, features",
)
def trip(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    trip_id: str,
    service_date: date,
    include: Annotated[str | None, Query(description="`shape` adds the GeoJSON path")] = None,
) -> schema.Trip | JSONResponse:
    realtime_cache(response, limits)
    params = {"trip_id": trip_id, "service_date": service_date, "release_id": snap.release_id}
    row = snap.one("trip", params)
    if row is None:
        gone = snap.one("trip_gone", params)
        if gone is None:
            raise HTTPException(404, f"no trip {trip_id}")
        body = schema.GoneTrip(
            title="Gone",
            status=410,
            detail=f"{trip_id} is from an older timetable version",
            service_date=service_date,
            journey=schema.JourneyKey(namespace=gone["key_namespace"], key=gone["key"]),
            by_key=by_key_path(gone["key_namespace"], gone["key"], service_date),
        )
        return JSONResponse(
            body.model_dump(mode="json"), status_code=410, media_type="application/problem+json"
        )
    if not row["runs"]:
        raise HTTPException(404, f"{trip_id} does not run on {service_date}")
    journey = snap.one("trip_journey", params | {"namespaces": JOURNEY_NAMESPACES})
    realtime = snap.one("trip_realtime", params)
    parts = (
        snap.all("trip_parts", {"run_key": row["run_key"], "service_date": service_date})
        if row["run_key"] is not None
        else []
    )
    features = snap.all(
        "trip_features",
        {"trip_id": trip_id, "route_id": row["route_id"], "service_id": row["service_id"]},
    )
    shape = None
    if include == "shape" and row["shape_id"] is not None:
        found = snap.one("trip_shape", {"shape_id": row["shape_id"]})
        shape = None if found is None else found["geometry"]
    return schema.Trip(
        **envelope(snap),
        trip_id=trip_id,
        service_date=service_date,
        journey=None if journey is None else schema.JourneyKey.model_validate(journey),
        line=line(row),
        agency=row["agency"],
        trip_short_name=row["trip_short_name"],
        headsign=row["headsign"],
        wheelchair_accessible=row["wheelchair_accessible"],
        bikes_allowed=row["bikes_allowed"],
        realtime=None if realtime is None else schema.TripRealtime.model_validate(realtime),
        run_parts=[schema.TripPart.model_validate(p) for p in parts]
        or [schema.TripPart(trip_id=trip_id, run_part=None)],
        calls=[schema.TripCall.model_validate(c) for c in snap.all("trip_calls", params)],
        features=[
            schema.Feature(
                scope=f["scope"],
                kind=f["kind"],
                call_sequence=f["call_sequence"],
                call_sequence_to=f["call_sequence_to"],
                source_code=f["source_code"],
                note=None
                if f["note_kind"] is None
                else schema.Note(kind=f["note_kind"], label=f["note_label"], text=f["note_text"]),
            )
            for f in features
        ],
        shape=shape,
    )
