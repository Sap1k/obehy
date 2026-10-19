"""Current vehicles (BASE_PLAN.md section 30): positions of the active release, never older than
the limits' maximum age. Without the vehicle registry, a vehicle's ID is
`<source>:<source_vehicle_id>`."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, get_args

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.rows import DictRow

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, realtime_cache
from obehy.api.db import Snapshot
from obehy.api.limits import Limits
from obehy.api.params import ParameterError, parse_bbox, split_vehicle_id

router = APIRouter(tags=["vehicles"])

SHOWN = ["running", "positioning", "layover"]
OPTIONAL = {"unmatched", "not_in_service"}


def _rows(snap: Snapshot, limits: Limits, **filters: Any) -> list[DictRow]:
    params: dict[str, Any] = {
        "release_id": snap.release_id,
        "seen_since": snap.now - timedelta(seconds=limits.vehicle_max_age_s),
        "states": list(get_args(schema.VehicleState)),
        "west": None,
        "south": None,
        "east": None,
        "north": None,
        "source": None,
        "source_vehicle_id": None,
        "limit": limits.vehicles_limit,
    }
    return snap.all("vehicles", params | filters)


def _vehicle(r: DictRow) -> schema.Vehicle:
    return schema.Vehicle(
        vehicle_id=f"{r['source']}:{r['source_vehicle_id']}",
        feed=r["feed"],
        source=r["source"],
        state=r["status"],
        last_seen=r["last_seen"],
        latitude=r["latitude"],
        longitude=r["longitude"],
        bearing=r["bearing"],
        trip_id=r["trip_id"] if r["route_id"] is not None else None,
        service_date=r["service_date"] if r["route_id"] is not None else None,
        line=line(r) if r["route_id"] is not None else None,
        headsign=r["headsign"],
        delay_s=r["delay_s"],
    )


@router.get(
    "/vehicles",
    response_model=schema.Vehicles,
    summary="Vehicles in a viewport",
)
def vehicles(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    bbox: Annotated[str, Query(description="minlon,minlat,maxlon,maxlat")],
    include: Annotated[
        str | None, Query(description="Comma-separated: unmatched, not_in_service")
    ] = None,
) -> schema.Vehicles:
    realtime_cache(response, limits)
    box = parse_bbox(bbox, limits.vehicles_bbox_max_deg2)
    extra = {w for w in (include or "").split(",") if w}
    if not extra <= OPTIONAL:
        raise ParameterError(f"include takes {', '.join(sorted(OPTIONAL))}")
    rows = _rows(
        snap,
        limits,
        states=SHOWN + sorted(extra),
        west=box.west,
        south=box.south,
        east=box.east,
        north=box.north,
    )
    return schema.Vehicles(**envelope(snap), vehicles=[_vehicle(r) for r in rows])


@router.get(
    "/vehicles/{vehicle_id}",
    response_model=schema.VehicleDetail,
    responses={404: {"model": schema.Problem}},
    summary="One current vehicle",
)
def vehicle(
    response: Response, snap: SnapshotDep, limits: LimitsDep, vehicle_id: str
) -> schema.VehicleDetail:
    realtime_cache(response, limits)
    source, source_vehicle_id = split_vehicle_id(vehicle_id)
    rows = _rows(snap, limits, source=source, source_vehicle_id=source_vehicle_id)
    if not rows:
        raise HTTPException(404, f"no current vehicle {vehicle_id}")
    # One vehicle can be in both feeds' state (a source reporting buses and trains): the freshest.
    freshest = max(rows, key=lambda r: (r["last_seen"], r["feed"]))
    return schema.VehicleDetail(**envelope(snap), vehicle=_vehicle(freshest))
