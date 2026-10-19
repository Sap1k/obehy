"""The departure board of a stop place: scheduled and realtime, both feeds."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, realtime_cache
from obehy.api.params import ParameterError, schedule_spans
from obehy.realtime.times import PRAGUE, TimeError, instant

router = APIRouter(tags=["departures"])


@router.get(
    "/stop-places/{location_id}/departures",
    response_model=schema.Departures,
    responses={404: {"model": schema.Problem}},
    summary="Departures from a stop place and its boarding points",
)
def departures(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    location_id: str,
    start: Annotated[
        datetime | None,
        Query(alias="from", description="ISO 8601 with an offset; default now"),
    ] = None,
    minutes: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> schema.Departures:
    realtime_cache(response, limits)
    stop_row = snap.one("stop", {"location_id": location_id})
    if stop_row is None:
        raise HTTPException(404, f"no stop place {location_id}")
    stop = schema.Stop.model_validate(stop_row)
    try:
        begin = instant(start if start is not None else snap.now)
    except TimeError:
        raise ParameterError("from needs a UTC offset") from None
    window = min(minutes or limits.departures_minutes, limits.departures_minutes_max)
    until = instant(begin + timedelta(minutes=window))
    since = instant(begin - timedelta(minutes=limits.lookback_min))
    spans = schedule_spans(since, until)
    rows = snap.all(
        "departures",
        {
            "location_id": stop.location_id,
            "dates": spans.dates,
            "lo": spans.lo,
            "hi": spans.hi,
            "since": since,
            "start": begin,
            "until": until,
            "release_id": snap.release_id,
            "limit": min(limit or limits.departures_limit, limits.departures_limit_max),
        },
    )
    return schema.Departures(
        **envelope(snap),
        stop=stop,
        since=begin.astimezone(PRAGUE),
        until=until.astimezone(PRAGUE),
        departures=[
            schema.Departure(
                trip_id=r["trip_id"],
                service_date=r["service_date"],
                line=line(r),
                trip_short_name=r["trip_short_name"],
                headsign=r["headsign"],
                scheduled=r["scheduled"],
                estimated=r["estimated"],
                delay_s=r["delay_s"],
                status=r["status"],
                source_class=r["source_class"],
                stale=r["stale"],
                platform=r["platform"],
            )
            for r in rows
        ],
    )
