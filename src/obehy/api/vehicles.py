"""Vehicles (BASE_PLAN.md sections 27 and 30): positions of the active release, never older
than the limits' maximum age, and what is known about each vehicle.

The worker decides which vehicle each source report is (the registry's, through a key: a
source's vehicle ID or the licence plate it reports; else the report's own
`<source>:<source_vehicle_id>`) and what is known about it (`rt.vehicle_profile`). Two
sources reporting one registered vehicle are one vehicle: its freshest report, every source
named. This module only reads.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, cast, get_args

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.rows import DictRow

from obehy.api import schema
from obehy.api.codes import load_codes
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, realtime_cache, traits
from obehy.api.db import Snapshot
from obehy.api.limits import Limits
from obehy.api.params import ParameterError, parse_bbox, parse_near, search_words
from obehy.reference.vocabulary import load_vocabulary

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
        "vehicle_id": None,
        "text": None,
        "words": [],
        "lat": None,
        "lon": None,
        "limit": limits.vehicles_limit,
    }
    return snap.all("vehicles", params | filters)


def _vehicle(r: DictRow) -> schema.Vehicle:
    on_trip = r["route_id"] is not None
    return schema.Vehicle(
        vehicle_id=r["vehicle_id"],
        feed=r["feed"],
        source=r["source"],
        state=r["status"],
        last_seen=r["last_seen"],
        latitude=r["latitude"],
        longitude=r["longitude"],
        bearing=r["bearing"],
        trip_id=r["trip_id"] if on_trip else None,
        service_date=r["service_date"] if on_trip else None,
        line=line(r) if on_trip else None,
        trip_short_name=r["trip_short_name"] if on_trip else None,
        headsign=r["headsign"],
        delay_s=r["delay_s"],
        traits=traits(r),
        sources=sorted(set(r["sources"])),
        distance_m=None if r["distance_m"] is None else round(r["distance_m"]),
    )


@router.get(
    "/vehicles",
    response_model=schema.Vehicles,
    summary="Vehicles in a viewport, or found by line, train number, headsign or number",
)
def vehicles(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    bbox: Annotated[str | None, Query(description="minlon,minlat,maxlon,maxlat")] = None,
    q: Annotated[
        str | None,
        Query(
            description="Line, train number, headsign, fleet number or plate, regardless of "
            "case and diacritics: an exact line or train number first, then the number or "
            "plate, a line it starts, any match"
        ),
    ] = None,
    near: Annotated[
        str | None, Query(description="lat,lon: with `q`, the nearest first in each rank")
    ] = None,
    limit: Annotated[int | None, Query(ge=1, description="With `q`")] = None,
    include: Annotated[
        str | None, Query(description="Comma-separated: unmatched, not_in_service")
    ] = None,
) -> schema.Vehicles:
    realtime_cache(response, limits)
    if (bbox is None) == (q is None):
        raise ParameterError("give exactly one of bbox and q")
    extra = {w for w in (include or "").split(",") if w}
    if not extra <= OPTIONAL:
        raise ParameterError(f"include takes {', '.join(sorted(OPTIONAL))}")
    states = SHOWN + sorted(extra)
    if q is not None:
        words = search_words(q)
        if not words:
            raise ParameterError("q needs a letter or digit")
        lat, lon = parse_near(near) if near is not None else (None, None)
        rows = _rows(
            snap,
            limits,
            states=states,
            text=" ".join(words),
            words=words,
            lat=lat,
            lon=lon,
            limit=min(limit or limits.vehicles_search_limit, limits.vehicles_search_limit_max),
        )
    else:
        assert bbox is not None
        box = parse_bbox(bbox, limits.vehicles_bbox_max_deg2)
        rows = _rows(
            snap,
            limits,
            states=states,
            west=box.west,
            south=box.south,
            east=box.east,
            north=box.north,
        )
    return schema.Vehicles(**envelope(snap), vehicles=[_vehicle(r) for r in rows])


@router.get(
    "/vehicles/{vehicle_id:path}",
    response_model=schema.VehicleDetail,
    responses={404: {"model": schema.Problem}},
    summary="A vehicle: its current state, registry entry, source reports and attributes",
)
def vehicle(
    response: Response, snap: SnapshotDep, limits: LimitsDep, vehicle_id: str
) -> schema.VehicleDetail:
    realtime_cache(response, limits)
    found = snap.one("vehicle_profile", {"vehicle_id": vehicle_id, "release_id": snap.release_id})
    assert found is not None  # one row: the profile, if any, and the reports
    if found["registered"] is None and not found["reports"]:
        raise HTTPException(404, f"no vehicle {vehicle_id}")
    current = _rows(snap, limits, vehicle_id=vehicle_id)
    vocabulary = load_vocabulary()
    codes = load_codes()
    registry = None
    if found["registered"]:
        registry = schema.RegistryEntry(
            vehicle_id=vehicle_id,
            public_label=found["public_label"],
            model=found["model"],
            manufacturer=found["manufacturer"],
            datasets=found["datasets"],
        )
    attributes: list[schema.VehicleAttributeValue] = []
    stored = cast(list[dict[str, Any]], found["attributes"] or [])
    for a in stored:
        known = vocabulary.attributes.get(a["attribute"])
        attributes.append(
            schema.VehicleAttributeValue.model_validate(
                a
                | {
                    "label": None if known is None else {"cs": known.cs, "en": known.en},
                    "icon": None if known is None else known.icon,
                }
            )
        )
    return schema.VehicleDetail(
        **envelope(snap),
        vehicle_id=vehicle_id,
        vehicle=_vehicle(current[0]) if current else None,
        registry=registry,
        reports=[
            schema.VehicleSource.model_validate(r | {"label": codes.source(r["source"])})
            for r in found["reports"]
        ],
        sources=list(found["sources"] or []),
        attributes=attributes,
    )
