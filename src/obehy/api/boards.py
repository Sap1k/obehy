"""Departure and arrival boards of one or several stop places: scheduled and realtime, both
feeds, with the vehicle on each trip and a cursor for the following rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.rows import DictRow

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, realtime_cache
from obehy.api.db import Snapshot
from obehy.api.limits import Limits
from obehy.api.params import (
    Cursor,
    ParameterError,
    decode_cursor,
    encode_cursor,
    schedule_spans,
)
from obehy.realtime.times import PRAGUE, TimeError, instant

router = APIRouter(tags=["boards"])

Start = Annotated[
    datetime | None, Query(alias="from", description="ISO 8601 with an offset; default now")
]
Minutes = Annotated[int | None, Query(ge=1)]
Limit = Annotated[int | None, Query(ge=1)]
CursorParam = Annotated[str | None, Query(alias="cursor", description="`next` of a previous page")]


def _stops(snap: Snapshot, ids: list[str]) -> list[schema.Stop]:
    """The stop places named (a boarding point names its stop place), in the order given."""

    found: dict[str, schema.Stop] = {}
    for location_id in ids:
        row = snap.one("stop", {"location_id": location_id})
        if row is None:
            raise HTTPException(404, f"no stop place {location_id}")
        stop = schema.Stop.model_validate(row)
        found.setdefault(stop.location_id, stop)
    return list(found.values())


def _vehicle(r: DictRow) -> schema.TripVehicle | None:
    if r["vehicle_id"] is None:
        return None
    return schema.TripVehicle(
        vehicle_id=r["vehicle_id"],
        state=r["vehicle_state"],
        last_seen=r["vehicle_seen"],
        latitude=r["vehicle_latitude"],
        longitude=r["vehicle_longitude"],
        bearing=r["vehicle_bearing"],
    )


def _board(
    snap: Snapshot,
    limits: Limits,
    stops: list[schema.Stop],
    *,
    arrivals: bool,
    start: datetime | None,
    minutes: int | None,
    limit: int | None,
    cursor: str | None,
) -> tuple[datetime, datetime, list[schema.Departure], str | None]:
    try:
        begin = instant(start if start is not None else snap.now)
    except TimeError:
        raise ParameterError("from needs a UTC offset") from None
    window = min(minutes or limits.departures_minutes, limits.departures_minutes_max)
    until = instant(begin + timedelta(minutes=window))
    since = instant(begin - timedelta(minutes=limits.lookback_min))
    rows_wanted = min(limit or limits.departures_limit, limits.departures_limit_max)
    after = None if cursor is None else decode_cursor(cursor)
    spans = schedule_spans(since, until)
    params: dict[str, Any] = {
        "locations": [s.location_id for s in stops],
        "arrivals": arrivals,
        "dates": spans.dates,
        "lo": spans.lo,
        "hi": spans.hi,
        "since": since,
        "start": begin,
        "until": until,
        "release_id": snap.release_id,
        "seen_since": snap.now - timedelta(seconds=limits.vehicle_max_age_s),
        "after_expected": None if after is None else after.expected,
        "after_scheduled": None if after is None else after.scheduled,
        "after_trip": None if after is None else after.trip_id,
        "after_date": None if after is None else after.service_date,
        "after_location": None if after is None else after.location_id,
        "limit": rows_wanted,
    }
    rows = snap.all("board", params)
    board = [
        schema.Departure(
            trip_id=r["trip_id"],
            service_date=r["service_date"],
            location_id=r["location_id"],
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
            vehicle=_vehicle(r),
        )
        for r in rows
    ]
    following = None
    if len(rows) == rows_wanted:
        last = rows[-1]
        following = encode_cursor(
            Cursor(
                last["expected"],
                last["scheduled"],
                last["trip_id"],
                last["service_date"],
                last["location_id"],
            )
        )
    return begin.astimezone(PRAGUE), until.astimezone(PRAGUE), board, following


def _ids(stops: str) -> list[str]:
    return [s for s in (part.strip() for part in stops.split(",")) if s]


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
    start: Start = None,
    minutes: Minutes = None,
    limit: Limit = None,
    cursor: CursorParam = None,
) -> schema.Departures:
    return _departures(response, snap, limits, [location_id], start, minutes, limit, cursor)


@router.get(
    "/departures",
    response_model=schema.Departures,
    responses={404: {"model": schema.Problem}},
    summary="One board of departures from several stop places",
)
def departures_of(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    stops: Annotated[str, Query(description="Comma-separated stop place IDs")],
    start: Start = None,
    minutes: Minutes = None,
    limit: Limit = None,
    cursor: CursorParam = None,
) -> schema.Departures:
    return _departures(response, snap, limits, _ids(stops), start, minutes, limit, cursor)


def _departures(
    response: Response,
    snap: Snapshot,
    limits: Limits,
    ids: list[str],
    start: datetime | None,
    minutes: int | None,
    limit: int | None,
    cursor: str | None,
) -> schema.Departures:
    realtime_cache(response, limits)
    stops = _checked(snap, limits, ids)
    since, until, rows, following = _board(
        snap,
        limits,
        stops,
        arrivals=False,
        start=start,
        minutes=minutes,
        limit=limit,
        cursor=cursor,
    )
    return schema.Departures(
        **envelope(snap),
        stop=stops[0],
        stops=stops,
        since=since,
        until=until,
        departures=rows,
        next=following,
    )


@router.get(
    "/stop-places/{location_id}/arrivals",
    response_model=schema.Arrivals,
    responses={404: {"model": schema.Problem}},
    summary="Arrivals at a stop place and its boarding points",
)
def arrivals(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    location_id: str,
    start: Start = None,
    minutes: Minutes = None,
    limit: Limit = None,
    cursor: CursorParam = None,
) -> schema.Arrivals:
    return _arrivals(response, snap, limits, [location_id], start, minutes, limit, cursor)


@router.get(
    "/arrivals",
    response_model=schema.Arrivals,
    responses={404: {"model": schema.Problem}},
    summary="One board of arrivals at several stop places",
)
def arrivals_of(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    stops: Annotated[str, Query(description="Comma-separated stop place IDs")],
    start: Start = None,
    minutes: Minutes = None,
    limit: Limit = None,
    cursor: CursorParam = None,
) -> schema.Arrivals:
    return _arrivals(response, snap, limits, _ids(stops), start, minutes, limit, cursor)


def _arrivals(
    response: Response,
    snap: Snapshot,
    limits: Limits,
    ids: list[str],
    start: datetime | None,
    minutes: int | None,
    limit: int | None,
    cursor: str | None,
) -> schema.Arrivals:
    realtime_cache(response, limits)
    stops = _checked(snap, limits, ids)
    since, until, rows, following = _board(
        snap,
        limits,
        stops,
        arrivals=True,
        start=start,
        minutes=minutes,
        limit=limit,
        cursor=cursor,
    )
    return schema.Arrivals(
        **envelope(snap),
        stop=stops[0],
        stops=stops,
        since=since,
        until=until,
        arrivals=rows,
        next=following,
    )


def _checked(snap: Snapshot, limits: Limits, ids: list[str]) -> list[schema.Stop]:
    if not ids:
        raise ParameterError("name at least one stop place")
    if len(ids) > limits.board_stops_max:
        raise ParameterError(f"at most {limits.board_stops_max} stop places")
    return _stops(snap, ids)
