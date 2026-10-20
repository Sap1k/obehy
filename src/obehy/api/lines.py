"""Lines (routes of the active release): search, stop patterns, the trips of a date and their
punctuality from history."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, realtime_cache, static_cache
from obehy.api.db import Snapshot
from obehy.api.params import ParameterError, date_span, search_words
from obehy.api.trips import JOURNEY_NAMESPACES
from obehy.realtime.times import local_date

router = APIRouter(tags=["lines"])

DateParam = Annotated[date | None, Query(alias="date", description="Service date; default today")]


def _line(snap: Snapshot, route_id: str) -> tuple[schema.Line, str | None, str | None]:
    row = snap.one("line", {"route_id": route_id})
    if row is None:
        raise HTTPException(404, f"no line {route_id}")
    return line(row), row["agency"], row["description"]


@router.get("/lines", response_model=schema.Lines, summary="Search lines by name")
def lines(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    q: Annotated[str, Query(description="Line number or name, regardless of diacritics")],
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> schema.Lines | Response:
    cached = static_cache(request, response, snap, limits)
    if cached is not None:
        return cached
    words = search_words(q)
    if not words:
        raise ParameterError("q needs a letter or digit")
    rows = snap.all(
        "lines_search",
        {
            "words": words,
            "first": words[0],
            "text": " ".join(words),
            "limit": min(limit or limits.lines_search_limit, limits.lines_search_limit_max),
        },
    )
    return schema.Lines(
        **envelope(snap),
        lines=[schema.LineSummary(line=line(r), agency=r["agency"]) for r in rows],
    )


@router.get(
    "/lines/{route_id}",
    response_model=schema.LineDetail,
    responses={404: {"model": schema.Problem}},
    summary="A line with the stop sequences its trips run on a date",
)
def line_detail(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    route_id: str,
    service_date: DateParam = None,
    include: Annotated[str | None, Query(description="`shape` adds each pattern's path")] = None,
) -> schema.LineDetail | Response:
    cached = static_cache(request, response, snap, limits)
    if cached is not None:
        return cached
    day = service_date or local_date(snap.now)
    found, agency, description = _line(snap, route_id)
    rows = snap.all(
        "line_patterns",
        {"route_id": route_id, "service_date": day, "limit": limits.line_patterns},
    )
    ids = sorted({location for r in rows for location in r["stops"]})
    places = {r["location_id"]: r for r in snap.all("locations", {"location_ids": ids})}
    shapes = {}
    if include == "shape":
        wanted = sorted({r["shape_id"] for r in rows if r["shape_id"] is not None})
        shapes = {s["shape_id"]: s["geometry"] for s in snap.all("shapes", {"shape_ids": wanted})}
    return schema.LineDetail(
        **envelope(snap),
        line=found,
        agency=agency,
        description=description,
        service_date=day,
        patterns=[
            schema.Pattern(
                headsign=r["headsign"],
                trips=r["trips"],
                stops=[
                    schema.PatternStop.model_validate(places[location])
                    for location in r["stops"]
                    if location in places
                ],
                shape=shapes.get(r["shape_id"]),
            )
            for r in rows
        ],
    )


@router.get(
    "/lines/{route_id}/trips",
    response_model=schema.LineTrips,
    responses={404: {"model": schema.Problem}},
    summary="A line's trips running on a date, with their realtime state",
)
def line_trips(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    route_id: str,
    service_date: DateParam = None,
) -> schema.LineTrips:
    realtime_cache(response, limits)
    day = service_date or local_date(snap.now)
    found, _, _ = _line(snap, route_id)
    rows = snap.all(
        "line_trips",
        {"route_id": route_id, "service_date": day, "release_id": snap.release_id},
    )
    return schema.LineTrips(
        **envelope(snap),
        line=found,
        service_date=day,
        trips=[schema.LineTrip.model_validate(r | {"service_date": day}) for r in rows],
    )


@router.get(
    "/lines/{route_id}/punctuality",
    response_model=schema.LinePunctuality,
    responses={404: {"model": schema.Problem}},
    summary="How punctual a line's departures were, from history",
)
def line_punctuality(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    route_id: str,
    since: Annotated[date | None, Query(description="First service date")] = None,
    until: Annotated[date | None, Query(description="Last service date; default today")] = None,
) -> schema.LinePunctuality:
    realtime_cache(response, limits)
    start, end = date_span(
        since,
        until,
        local_date(snap.now),
        limits.history_default_days,
        limits.history_max_days,
    )
    found, _, _ = _line(snap, route_id)
    rows = snap.all(
        "line_punctuality",
        {
            "route_id": route_id,
            "namespaces": JOURNEY_NAMESPACES,
            "since": start,
            "until": end,
            "early_s": limits.on_time_early_s,
            "late_s": limits.on_time_late_s,
        },
    )
    overall = next(r for r in rows if r["grouping"] == "all")

    def buckets(grouping: str) -> list[schema.PunctualityBucket]:
        return [
            schema.PunctualityBucket(
                key=r["key"],
                name=r["name"],
                events=r["events"],
                on_time_share=r["on_time_share"],
                mean_delay_s=r["mean_delay_s"],
            )
            for r in rows
            if r["grouping"] == grouping
        ]

    return schema.LinePunctuality(
        **envelope(snap),
        line=found,
        since=start,
        until=end,
        journeys=overall["journeys"],
        events=overall["events"],
        on_time_share=overall["on_time_share"],
        by_hour=buckets("hour"),
        by_stop=buckets("stop"),
    )
