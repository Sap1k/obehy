"""Lines (routes of the active release): search; a line's overview (validity, its own features
and notes, the date's trips and stop patterns); its printed timetable (laid out by
`timetable.py`); the trips of a date with their realtime state and vehicles. How punctual a
journey is lives with its trip (`trips/{trip_id}/history`): a line's journeys differ too much
to share one figure."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.rows import DictRow

from obehy.api import schema, timetable
from obehy.api.codes import (
    Codes,
    amenities,
    call_flags,
    features,
    load_codes,
    tristate,
    wheelchair_of,
)
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
from obehy.api.params import ParameterError, search_words
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
    """The date's trips (`line_patterns.sql`) by stop sequence, most used first."""

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
    trips = snap.all("line_patterns", {"route_id": route_id, "service_date": day})
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


def _cell(
    codes: Codes, call: dict[str, Any], stated: list[schema.Feature], *, first: bool, last: bool
) -> schema.TimetableCell:
    if not call["passenger"]:
        return schema.TimetableCell(
            passes=True,
            arrival_s=call["arrival_s"],
            departure_s=call["departure_s"],
            arrival=call["arrival"],
            departure=call["departure"],
            boarding="none",
            alighting="none",
            flags=[],
            platform=call["platform"],
        )
    boarding = codes.boarding_of(call["pickup_type"])
    alighting = codes.boarding_of(call["dropoff_type"])
    return schema.TimetableCell(
        passes=False,
        arrival_s=call["arrival_s"],
        departure_s=call["departure_s"],
        arrival=call["arrival"],
        departure=call["departure"],
        boarding=boarding,
        alighting=alighting,
        flags=call_flags(stated, call["sequence"], boarding, alighting, first=first, last=last),
        platform=call["platform"],
    )


def _column(
    codes: Codes,
    trip: DictRow,
    calls: list[dict[str, Any]],
    rows: list[timetable.Row],
    order: list[timetable.Row],
    stated: list[schema.Feature],
    route: list[schema.Feature],
) -> schema.TimetableTrip:
    passengers = [c for c in calls if c["passenger"]]
    at = {row: n for n, row in enumerate(order)}
    cells: list[schema.TimetableCell | None] = [None] * len(order)
    for call, row in zip(calls, rows, strict=True):
        cells[at[row]] = _cell(
            codes, call, stated, first=call is passengers[0], last=call is passengers[-1]
        )
    whole = amenities([*route, *stated])
    return schema.TimetableTrip(
        trip_id=trip["trip_id"],
        trip_short_name=trip["trip_short_name"],
        headsign=trip["headsign"],
        service_id=trip["service_id"],
        operates=whole.operates,
        wheelchair=whole.wheelchair or wheelchair_of([], tristate(trip["wheelchair_accessible"])),
        bicycles=whole.bicycles,
        features=stated,
        notes=[f.note for f in stated if f.note is not None],
        cells=cells,
    )


def _start(calls: list[dict[str, Any]]) -> int:
    """When a trip leaves its first passenger call, in seconds of the service day."""

    first = next(c for c in calls if c["passenger"])
    return first["departure_s"] if first["departure_s"] is not None else first["arrival_s"]


@router.get(
    "/lines/{route_id}/timetable",
    response_model=schema.LineTimetable,
    responses={404: {"model": schema.Problem}},
    summary="A line's printed timetable: per direction, every trip with its codes and calls",
)
def line_timetable(
    request: Request,
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    route_id: str,
    service_date: Annotated[
        date | None, Query(alias="date", description="Only the trips of this date; default all")
    ] = None,
) -> schema.LineTimetable | Response:
    cached = static_cache(request, response, snap, limits)
    if cached is not None:
        return cached
    codes = load_codes()
    found, _, _ = _line(snap, route_id)
    validity = snap.one("line_validity", {"route_id": route_id})
    assert validity is not None  # an aggregate: one row
    route = features(codes, snap.all("line_features", {"route_id": route_id}))
    span = {"since": service_date or local_date(snap.now), "until": service_date or date.max}
    trips = snap.all(
        "line_timetable", span | {"route_id": route_id, "dated": service_date is not None}
    )
    # A trip's rows: its passenger calls, and the stops it runs through where another trip of
    # the line stops (a railway point no trip stops at is no row).
    stopping = {c["location_id"] for t in trips for c in t["calls"] if c["passenger"]}
    calls = {
        t["trip_id"]: [c for c in t["calls"] if c["passenger"] or c["location_id"] in stopping]
        for t in trips
    }
    trips = [t for t in trips if any(c["passenger"] for c in calls[t["trip_id"]])]
    rows = {
        t["trip_id"]: timetable.rows_of([c["location_id"] for c in calls[t["trip_id"]]])
        for t in trips
    }
    by_trip: dict[str, list[DictRow]] = {}
    for r in snap.all("line_trip_features", {"trip_ids": [t["trip_id"] for t in trips]}):
        by_trip.setdefault(r["trip_id"], []).append(r)
    names = {c["location_id"]: c["name"] for t in trips for c in calls[t["trip_id"]]}
    at_stop: dict[str, list[DictRow]] = {}
    for r in snap.all("location_features", {"location_ids": sorted(names)}):
        at_stop.setdefault(r["location_id"], []).append(r)
    stop_features = {stop: features(codes, stated) for stop, stated in at_stop.items()}
    directions: list[schema.TimetableDirection] = []
    for members in timetable.directions([(t["direction"], rows[t["trip_id"]]) for t in trips]):
        group = sorted(
            (trips[n] for n in members),
            key=lambda t: (_start(calls[t["trip_id"]]), t["trip_id"]),
        )
        order = timetable.merge([rows[t["trip_id"]] for t in group])
        directions.append(
            schema.TimetableDirection(
                direction=group[0]["direction"],
                headsign=_most_common([t["headsign"] for t in group]),
                stops=[
                    schema.TimetableStop(
                        location_id=stop,
                        name=names[stop],
                        occurrence=occurrence,
                        features=stop_features.get(stop, []),
                    )
                    for stop, occurrence in order
                ],
                trips=[
                    _column(
                        codes,
                        t,
                        calls[t["trip_id"]],
                        rows[t["trip_id"]],
                        order,
                        features(codes, by_trip.get(t["trip_id"], [])),
                        route,
                    )
                    for t in group
                ],
            )
        )
    return schema.LineTimetable(
        **envelope(snap),
        line=found,
        runs_from=validity["first_date"],
        runs_until=validity["last_date"],
        service_date=service_date,
        features=route,
        notes=[f.note for f in route if f.note is not None],
        calendars={
            r["service_id"]: r["dates"]
            for r in snap.all(
                "service_dates", span | {"service_ids": sorted({t["service_id"] for t in trips})}
            )
        },
        directions=directions,
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
