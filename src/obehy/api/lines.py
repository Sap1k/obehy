"""Lines (routes of the active release): search; a line's overview (validity, its own features
and notes, the date's trips and stop patterns); its timetable; the trips of a date with their
realtime state and vehicles; punctuality from history."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.rows import DictRow

from obehy.api import schema
from obehy.api.codes import features, load_codes, tristate
from obehy.api.common import (
    LimitsDep,
    SnapshotDep,
    envelope,
    line,
    realtime_cache,
    static_cache,
    trip_vehicle,
)
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


def _patterns(rows: list[DictRow], limit: int) -> list[list[DictRow]]:
    """The date's trips (`line_timetable.sql`) by stop sequence, most used first."""

    grouped: dict[tuple[str, ...], list[DictRow]] = {}
    for r in rows:
        grouped.setdefault(tuple(r["stops"]), []).append(r)
    ordered = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [trips for _, trips in ordered[:limit]]


def _most_common[T](values: Sequence[T | None]) -> T | None:
    counts = Counter(v for v in values if v is not None)
    return min(counts, key=lambda v: (-counts[v], str(v))) if counts else None


def _stops(snap: Snapshot, patterns: list[list[DictRow]]) -> dict[str, schema.PatternStop]:
    ids = sorted({location for trips in patterns for location in trips[0]["stops"]})
    return {
        r["location_id"]: schema.PatternStop.model_validate(r)
        for r in snap.all("locations", {"location_ids": ids})
    }


def _pattern(
    trips: list[DictRow], stops: dict[str, schema.PatternStop], shapes: dict[str, Any]
) -> schema.Pattern:
    starts = [t["times"][0] for t in trips if t["times"][0] is not None]
    shape_ids = sorted(t["shape_id"] for t in trips if t["shape_id"] is not None)
    return schema.Pattern(
        headsign=_most_common([t["headsign"] for t in trips]),
        direction=_most_common([t["direction"] for t in trips]),
        trips=len(trips),
        first_departure=min(starts, default=None),
        last_departure=max(starts, default=None),
        stops=[stops[s] for s in trips[0]["stops"] if s in stops],
        shape=shapes.get(shape_ids[0]) if shape_ids else None,
    )


@router.get(
    "/lines/{route_id}",
    response_model=schema.LineDetail,
    responses={404: {"model": schema.Problem}},
    summary="A line: validity, features and notes, and its trips and stop patterns on a date",
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
    validity = snap.one("line_validity", {"route_id": route_id})
    assert validity is not None  # an aggregate: one row
    stated = features(load_codes(), snap.all("line_features", {"route_id": route_id}))
    trips = snap.all("line_timetable", {"route_id": route_id, "service_date": day})
    patterns = _patterns(trips, limits.line_patterns)
    stops = _stops(snap, patterns)
    shapes = {}
    if include == "shape":
        wanted = sorted({t["shape_id"] for p in patterns for t in p if t["shape_id"] is not None})
        shapes = {s["shape_id"]: s["geometry"] for s in snap.all("shapes", {"shape_ids": wanted})}
    starts = [t["times"][0] for t in trips if t["times"][0] is not None]
    systems = {f.system.code: f.system for f in stated if f.system is not None}
    return schema.LineDetail(
        **envelope(snap),
        line=found,
        agency=agency,
        description=description,
        runs_from=validity["first_date"],
        runs_until=validity["last_date"],
        integrated_transport=[systems[code] for code in sorted(systems)],
        features=stated,
        notes=[f.note for f in stated if f.note is not None],
        service_date=day,
        day=schema.LineDay(
            trips=len(trips),
            first_departure=min(starts, default=None),
            last_departure=max(starts, default=None),
        ),
        patterns=[_pattern(p, stops, shapes) for p in patterns],
    )


@router.get(
    "/lines/{route_id}/timetable",
    response_model=schema.LineTimetable,
    responses={404: {"model": schema.Problem}},
    summary="A line's timetable on a date: per stop sequence, each trip's times at its stops",
)
def line_timetable(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    route_id: str,
    service_date: DateParam = None,
) -> schema.LineTimetable | Response:
    cached = static_cache(request, response, snap, limits)
    if cached is not None:
        return cached
    day = service_date or local_date(snap.now)
    found, _, _ = _line(snap, route_id)
    trips = snap.all("line_timetable", {"route_id": route_id, "service_date": day})
    patterns = _patterns(trips, limits.line_patterns)
    stops = _stops(snap, patterns)
    return schema.LineTimetable(
        **envelope(snap),
        line=found,
        service_date=day,
        patterns=[
            schema.TimetablePattern(
                headsign=_most_common([t["headsign"] for t in p]),
                direction=_most_common([t["direction"] for t in p]),
                stops=[stops[s] for s in p[0]["stops"] if s in stops],
                trips=[
                    schema.TimetableTrip(
                        trip_id=t["trip_id"],
                        trip_short_name=t["trip_short_name"],
                        headsign=t["headsign"],
                        wheelchair_accessible=tristate(t["wheelchair_accessible"]),
                        bikes_allowed=tristate(t["bikes_allowed"]),
                        times=[
                            time
                            for s, time in zip(t["stops"], t["times"], strict=True)
                            if s in stops
                        ],
                    )
                    for t in p
                ],
            )
            for p in patterns
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
    params = {"route_id": route_id, "service_date": day, "release_id": snap.release_id}
    rows = snap.all("line_trips", params)
    seen_since = snap.now - timedelta(seconds=limits.vehicle_max_age_s)
    on_trips: dict[str, list[schema.TripVehicle]] = {}
    for v in snap.all(
        "trip_vehicles",
        params | {"trip_ids": [r["trip_id"] for r in rows], "seen_since": seen_since},
    ):
        on_trips.setdefault(v["trip_id"], []).append(trip_vehicle(v))
    return schema.LineTrips(
        **envelope(snap),
        line=found,
        service_date=day,
        trips=[
            schema.LineTrip.model_validate(
                r | {"service_date": day, "vehicles": on_trips.get(r["trip_id"], [])}
            )
            for r in rows
        ],
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
