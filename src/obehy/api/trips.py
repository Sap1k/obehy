"""Trips by their feed trip ID and service date; source keys only as lookup aliases.

A trip ID lasts one timetable version of its line (BASE_PLAN.md section 6). One from an older
version answers 410 with the journey it ran as (from history), whose key `by-key` resolves
against the active release. A journey's recorded runs are under `journeys/…/history`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg.rows import DictRow

from obehy.api import schema
from obehy.api.alerts import find_alerts
from obehy.api.codes import amenities, call_flags, features, load_codes, tristate
from obehy.api.common import (
    LimitsDep,
    SnapshotDep,
    envelope,
    line,
    realtime_cache,
    trip_vehicle,
)
from obehy.api.params import date_span
from obehy.realtime.times import local_date

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


def _call(
    row: DictRow, features: list[schema.Feature], *, first: bool, last: bool
) -> schema.TripCall:
    codes = load_codes()
    boarding = codes.boarding_of(row["pickup_type"])
    alighting = codes.boarding_of(row["dropoff_type"])
    flags = call_flags(features, row["sequence"], boarding, alighting, first=first, last=last)
    return schema.TripCall(
        sequence=row["sequence"],
        location_id=row["location_id"],
        name=row["name"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        boarding=boarding,
        alighting=alighting,
        flags=flags,
        platform=row["platform"],
        scheduled_arrival=row["scheduled_arrival"],
        scheduled_departure=row["scheduled_departure"],
        estimated_arrival=row["estimated_arrival"],
        estimated_departure=row["estimated_departure"],
        status=row["status"],
        source_class=row["source_class"],
    )


def _realtime(row: DictRow) -> schema.TripRealtime:
    codes = load_codes()
    return schema.TripRealtime(
        lifecycle=row["lifecycle"],
        delay_s=row["delay_s"],
        off_route=row["off_route"],
        stale=row["stale"],
        updated_at=row["updated_at"],
        sources=[
            schema.RealtimeSource.model_validate(s | {"label": codes.source(s["source"])})
            for s in row["sources"]
        ],
    )


@router.get(
    "/trips/{trip_id}/{service_date}",
    response_model=schema.Trip,
    responses={404: {"model": schema.Problem}, 410: {"model": schema.GoneTrip}},
    summary="A trip on a service date: calls, realtime, amenities, connections",
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
    codes = load_codes()
    journey_row = snap.one("trip_journey", params | {"namespaces": JOURNEY_NAMESPACES})
    journey = None if journey_row is None else schema.JourneyKey.model_validate(journey_row)
    realtime = snap.one("trip_realtime", params)
    parts = (
        snap.all("trip_parts", {"run_key": row["run_key"], "service_date": service_date})
        if row["run_key"] is not None
        else []
    )
    stated = features(
        codes,
        snap.all(
            "trip_features",
            {"trip_id": trip_id, "route_id": row["route_id"], "service_id": row["service_id"]},
        ),
    )
    seen_since = snap.now - timedelta(seconds=limits.vehicle_max_age_s)
    vehicles = snap.all("trip_vehicles", params | {"trip_ids": [trip_id], "seen_since": seen_since})
    links = (
        []
        if journey is None
        else snap.all(
            "trip_links",
            {"namespace": journey.namespace, "key": journey.key, "service_date": service_date},
        )
    )
    calls = snap.all("trip_calls", params)
    shape = None
    if include == "shape" and row["shape_id"] is not None:
        found = snap.one("trip_shape", {"shape_id": row["shape_id"]})
        shape = None if found is None else found["geometry"]
    return schema.Trip(
        **envelope(snap),
        trip_id=trip_id,
        service_date=service_date,
        journey=journey,
        line=line(row),
        agency=row["agency"],
        trip_short_name=row["trip_short_name"],
        headsign=row["headsign"],
        wheelchair_accessible=tristate(row["wheelchair_accessible"]),
        bikes_allowed=tristate(row["bikes_allowed"]),
        amenities=amenities(stated),
        realtime=None if realtime is None else _realtime(realtime),
        vehicles=[trip_vehicle(v) for v in vehicles],
        run_parts=[schema.TripPart.model_validate(p) for p in parts]
        or [schema.TripPart(trip_id=trip_id, run_part=None)],
        calls=[
            _call(c, stated, first=n == 0, last=n == len(calls) - 1) for n, c in enumerate(calls)
        ],
        features=stated,
        notes=[f.note for f in stated if f.note is not None],
        connections=[
            schema.Connection(
                direction=c["direction"],
                at_sequence=c["at_sequence"],
                wait_minutes=c["wait_minutes"],
                line=c["line"],
                destination=c["destination"],
                note=c["note"],
                trip=None
                if c["target_trip_id"] is None
                else schema.TripRef(trip_id=c["target_trip_id"], service_date=service_date),
            )
            for c in snap.all("trip_connections", params)
        ],
        links=[
            schema.JourneyLink(
                kind=k["kind"],
                journey=schema.JourneyKey(namespace=k["namespace"], key=k["key"]),
                trip=None
                if k["trip_id"] is None
                else schema.TripRef(trip_id=k["trip_id"], service_date=service_date),
            )
            for k in links
        ],
        alerts=find_alerts(
            snap,
            limits,
            routes=[row["route_id"]],
            stops=sorted({c["location_id"] for c in calls}),
            trips=[trip_id],
        ),
        shape=shape,
    )


def _delay(actual: datetime | None, scheduled: datetime | None) -> int | None:
    if actual is None or scheduled is None:
        return None
    return round((actual - scheduled).total_seconds())


@router.get(
    "/journeys/{namespace}/{key}/history",
    response_model=schema.TripHistory,
    summary="A journey's recorded runs: actual times and delays per service date",
)
def journey_history(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    namespace: str,
    key: str,
    since: Annotated[date | None, Query(description="First service date")] = None,
    until: Annotated[date | None, Query(description="Last service date; default today")] = None,
) -> schema.TripHistory:
    realtime_cache(response, limits)
    start, end = date_span(
        since,
        until,
        local_date(snap.now),
        limits.history_default_days,
        limits.history_max_days,
    )
    rows = snap.all(
        "trip_history", {"namespace": namespace, "key": key, "since": start, "until": end}
    )
    days: dict[date, tuple[str, bool]] = {}  # per service date: its trip, whether cancelled
    calls: defaultdict[date, list[schema.HistoryCall]] = defaultdict(list)
    for r in rows:
        days.setdefault(r["service_date"], (r["trip_id"], r["cancelled"]))
        calls[r["service_date"]].append(
            schema.HistoryCall(
                location_id=r["location_id"],
                name=r["name"],
                scheduled_arrival=r["scheduled_arrival"],
                scheduled_departure=r["scheduled_departure"],
                actual_arrival=r["actual_arrival"],
                actual_departure=r["actual_departure"],
                arrival_delay_s=_delay(r["actual_arrival"], r["scheduled_arrival"]),
                departure_delay_s=_delay(r["actual_departure"], r["scheduled_departure"]),
            )
        )
    return schema.TripHistory(
        **envelope(snap),
        journey=schema.JourneyKey(namespace=namespace, key=key),
        days=[
            schema.HistoryDay(
                service_date=day, trip_id=trip_id, cancelled=cancelled, calls=calls[day]
            )
            for day, (trip_id, cancelled) in days.items()
        ],
    )
