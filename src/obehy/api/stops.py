"""Stops: name search, viewport, nearby, stop place detail."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response

from obehy.api import schema
from obehy.api.codes import features, load_codes, tristate
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, static_cache
from obehy.api.db import Snapshot
from obehy.api.params import ParameterError, parse_bbox, parse_near, search_words

router = APIRouter(tags=["stops"])


def group_members(snap: Snapshot, location_id: str) -> list[schema.Stop]:
    """The group of a stop place (a railway station and the stop in front of it, decided at
    activation, release/pairing.py): its members, stations first, each knowing the group;
    empty when it stands alone."""

    members = [
        schema.Stop.model_validate(r)
        for r in snap.all("stop_group_members", {"location_id": location_id})
    ]
    group = [
        schema.GroupMember(location_id=m.location_id, name=m.name, domain=m.domain, modes=m.modes)
        for m in members
    ]
    for m in members:
        m.group = group
    return members


@router.get(
    "/stops",
    response_model=schema.Stops,
    summary="Search stop places by name, list those in a viewport, or those near a point",
)
def stops(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    q: Annotated[str | None, Query(description="Name, regardless of case and diacritics")] = None,
    bbox: Annotated[str | None, Query(description="minlon,minlat,maxlon,maxlat")] = None,
    near: Annotated[str | None, Query(description="lat,lon; nearest first")] = None,
    radius: Annotated[int | None, Query(ge=1, description="Metres around `near`")] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> schema.Stops | Response:
    if sum(v is not None for v in (q, bbox, near)) != 1:
        raise ParameterError("give exactly one of q, bbox and near")
    cached = static_cache(request, response, snap, limits)
    if cached is not None:
        return cached
    if q is not None:
        words = search_words(q)
        if sum(len(w) for w in words) < limits.search_min_chars:
            raise ParameterError(f"q needs at least {limits.search_min_chars} letters or digits")
        rows = snap.all(
            "stops_search",
            {
                "words": words,
                "first": words[0],
                "text": " ".join(words),
                "limit": min(limit or limits.search_limit, limits.search_limit_max),
            },
        )
    elif near is not None:
        lat, lon = parse_near(near)
        metres = min(radius or limits.near_radius_m, limits.near_radius_max_m)
        rows = snap.all(
            "stops_near",
            {
                "lat": lat,
                "lon": lon,
                "radius": metres,
                # A box safely larger than the radius at Czech latitudes (cos 51° ≈ 0.63).
                "degrees": metres / 111_320 / 0.6,
                "limit": min(limit or limits.near_limit, limits.near_limit),
            },
        )
    else:
        assert bbox is not None
        box = parse_bbox(bbox, limits.stops_bbox_max_deg2)
        rows = snap.all(
            "stops_bbox",
            {
                "west": box.west,
                "south": box.south,
                "east": box.east,
                "north": box.north,
                "limit": min(limit or limits.stops_bbox_limit, limits.stops_bbox_limit),
            },
        )
    return schema.Stops(**envelope(snap), stops=[schema.Stop.model_validate(r) for r in rows])


@router.get(
    "/stop-places/{location_id}",
    response_model=schema.StopPlace,
    responses={404: {"model": schema.Problem}},
    summary="A stop place with its facilities, boarding points and lines",
)
def stop_place(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    location_id: str,
    include: Annotated[
        str | None, Query(description="`lines` adds every line calling here (a hub has hundreds)")
    ] = None,
) -> schema.StopPlace | Response:
    cached = static_cache(request, response, snap, limits)
    if cached is not None:
        return cached
    rows = snap.all("stops_by_id", {"location_ids": [location_id]})
    if not rows:
        raise HTTPException(404, f"no stop place {location_id}")
    row = rows[0]
    stop = schema.Stop.model_validate(row)
    members = group_members(snap, stop.location_id)
    if members:
        stop.group = members[0].group
    params = {"location_id": stop.location_id}
    codes = load_codes()
    return schema.StopPlace(
        **envelope(snap),
        stop=stop,
        wheelchair_boarding=tristate(row["wheelchair_boarding"]),
        facilities=features(codes, snap.all("stop_features", params)),
        boarding_points=[
            schema.BoardingPoint.model_validate(r) for r in snap.all("stop_boarding_points", params)
        ],
        lines=[line(r) for r in snap.all("stop_lines", params)] if include == "lines" else None,
    )
