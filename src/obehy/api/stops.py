"""Stops: name search, viewport, stop place detail."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, static_cache
from obehy.api.params import ParameterError, parse_bbox, search_words

router = APIRouter(tags=["stops"])


@router.get(
    "/stops",
    response_model=schema.Stops,
    summary="Search stop places by name, or list those in a viewport",
)
def stops(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    q: Annotated[str | None, Query(description="Name, regardless of case and diacritics")] = None,
    bbox: Annotated[str | None, Query(description="minlon,minlat,maxlon,maxlat")] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> schema.Stops | Response:
    if (q is None) == (bbox is None):
        raise ParameterError("give exactly one of q and bbox")
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
    summary="A stop place with its boarding points and lines",
)
def stop_place(
    request: Request, response: Response, snap: SnapshotDep, limits: LimitsDep, location_id: str
) -> schema.StopPlace | Response:
    cached = static_cache(request, response, snap, limits)
    if cached is not None:
        return cached
    row = snap.one("stop", {"location_id": location_id})
    if row is None:
        raise HTTPException(404, f"no stop place {location_id}")
    stop = schema.Stop.model_validate(row)
    params = {"location_id": stop.location_id}
    return schema.StopPlace(
        **envelope(snap),
        stop=stop,
        boarding_points=[
            schema.BoardingPoint.model_validate(r) for r in snap.all("stop_boarding_points", params)
        ],
        lines=[line(r) for r in snap.all("stop_lines", params)],
    )
