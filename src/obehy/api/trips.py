"""Trips by their feed trip ID and service date; source keys only as lookup aliases.

A trip ID lasts one timetable version of its line (BASE_PLAN.md section 6). One from an older
version answers 410 with the journey it ran as (from history), whose key `by-key` resolves
against the active release. A trip's `history` is its journey's recorded runs, under every trip
ID that journey had; its `circulation` the oběh it was part of (`circulations.py`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg.rows import DictRow

from obehy.api import punctuality, schema
from obehy.api.alerts import find_alerts, trip_alerts
from obehy.api.codes import amenities, call_flags, features, load_codes, tristate
from obehy.api.common import (
    LimitsDep,
    SnapshotDep,
    envelope,
    line,
    realtime_cache,
    seen_since,
    today,
    trip_vehicle,
)
from obehy.api.db import Snapshot
from obehy.api.limits import Limits
from obehy.api.lookup_rules import can_ask
from obehy.api.params import date_span
from obehy.api.resolve import TripOnDate, by_key, gone, recorded_journey, trip_on
from obehy.api.segments import on_time, percentile, run, summary

router = APIRouter(tags=["trips"])


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
    found = by_key(snap, namespace, key, service_date)
    if found.trip_id is None:
        raise HTTPException(404 if found.outcome == "not_found" else 409, found.detail)
    return RedirectResponse(trip_path(found.trip_id, service_date), status_code=308)


def _passed(row: DictRow, last: bool, now: datetime, after: timedelta) -> bool:
    """Whether the vehicle has left the call: realtime says so, else its expected (or
    scheduled) time is `after` behind; a cancelled call never is."""

    if row["status"] == "cancelled":
        return False
    if row["status"] == "actual" or row["live_passed"]:
        return True
    scheduled, estimated = punctuality.rider_time(
        (row["scheduled_arrival"], row["estimated_arrival"]),
        (row["scheduled_departure"], row["estimated_departure"]),
        last=last,
    )
    at = estimated or scheduled
    return at is not None and at < now - after


def trip_call(
    row: DictRow,
    features: list[schema.Feature],
    *,
    first: bool,
    last: bool,
    stale: bool,
    passed: bool,
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
        **asdict(
            punctuality.of_time(
                *punctuality.rider_time(
                    (row["scheduled_arrival"], row["estimated_arrival"]),
                    (row["scheduled_departure"], row["estimated_departure"]),
                    last=last,
                ),
                row["status"],
                stale=stale,
            )
        ),
        passed=passed,
    )


def next_sequence(snap: Snapshot, limits: Limits, trip: TripOnDate) -> int | None:
    """The sequence of the trip's first call not passed."""

    params = {
        "trip_id": trip.trip_id,
        "service_date": trip.service_date,
        "release_id": snap.release_id,
    }
    rows = snap.all("trip_calls", params)
    after = timedelta(seconds=limits.passed_after_s)
    return next(
        (
            r["sequence"]
            for n, r in enumerate(rows)
            if not _passed(r, n == len(rows) - 1, snap.now, after)
        ),
        None,
    )


def next_call(calls: list[schema.TripCall]) -> int | None:
    """The index of the first call not passed."""

    return next((n for n, c in enumerate(calls) if not c.passed), None)


def trip_realtime(row: DictRow) -> schema.TripRealtime:
    codes = load_codes()
    return schema.TripRealtime(
        lifecycle=row["lifecycle"],
        **asdict(punctuality.of_seconds(row["delay_s"], stale=row["stale"])),
        off_route=row["off_route"],
        stale=row["stale"],
        updated_at=row["updated_at"],
        sources=[
            schema.RealtimeSource.model_validate(
                s | {"label": codes.source(s["source"]), "reported_delay_s": s["delay_s"]}
            )
            for s in row["sources"]
        ],
    )


def _delay(actual: datetime | None, scheduled: datetime | None) -> int | None:
    if actual is None or scheduled is None:
        return None
    return round((actual - scheduled).total_seconds())


def _stops(days: list[schema.HistoryDay]) -> list[schema.HistoryStop]:
    """Per stop, how late the journey was over the days: its departure, at its last call its
    arrival. Stops in the call order of the latest day that calls there."""

    names: dict[str, str] = {}
    delays: defaultdict[str, list[int]] = defaultdict(list)
    for day in days:
        for n, call in enumerate(day.calls):
            names.setdefault(call.location_id, call.name)
            last = n == len(day.calls) - 1
            delay = call.arrival_delay_s if last else call.departure_delay_s
            if delay is not None:
                delays[call.location_id].append(delay)
    stops: list[schema.HistoryStop] = []
    for location_id, name in names.items():
        found = sorted(delays[location_id])
        punctual = [d for d in found if on_time(d)]
        stops.append(
            schema.HistoryStop(
                location_id=location_id,
                name=name,
                runs=len(found),
                on_time_share=len(punctual) / len(found) if found else None,
                median_delay_s=percentile(found, 0.5) if found else None,
                p90_delay_s=percentile(found, 0.9) if found else None,
                max_delay_s=found[-1] if found else None,
            )
        )
    return stops


# Before the trip itself: `/trips/{trip_id}/{service_date}` would read `history` as a date.
@router.get(
    "/trips/{trip_id}/history",
    response_model=schema.TripHistory,
    responses={404: {"model": schema.Problem}},
    summary="A trip's journey as it ran: actual times and delays per date, and per stop",
)
def trip_history(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    trip_id: str,
    since: Annotated[date | None, Query(description="First service date")] = None,
    until: Annotated[date | None, Query(description="Last service date; default today")] = None,
    board: Annotated[
        str | None,
        Query(
            alias="from",
            description="The segment's first stop (a `stops` location ID, e.g. where the trip "
            "was opened from); the journey's first stop when it is none of them",
        ),
    ] = None,
    alight: Annotated[
        str | None,
        Query(alias="to", description="Its last stop; the journey's last unless after `from`"),
    ] = None,
) -> schema.TripHistory:
    realtime_cache(response, limits)
    start, end = date_span(
        since,
        until,
        today(snap),
        limits.history_default_days,
        limits.history_max_days,
    )
    span = {"since": start, "until": end}
    journey = recorded_journey(snap, trip_id, start, end)
    if journey is None:
        raise HTTPException(404, f"no journey of {trip_id} is known")
    keyed = span | {"namespace": journey.namespace, "key": journey.key}
    worked_by: defaultdict[date, list[schema.HistoryVehicle]] = defaultdict(list)
    for r in snap.all("journey_vehicles", keyed):
        worked_by[r["service_date"]].append(
            schema.HistoryVehicle(
                vehicle_id=r["vehicle_id"],
                source=r["source"],
                source_vehicle_id=r["source_vehicle_id"],
                fleet_number=r["fleet_number"],
                public_label=r["public_label"],
                physical=r["physical"],
            )
        )
    seen: dict[date, tuple[str, bool]] = {}  # per service date: its trip, whether cancelled
    calls: defaultdict[date, list[schema.HistoryCall]] = defaultdict(list)
    for r in snap.all("trip_history", keyed):
        seen.setdefault(r["service_date"], (r["trip_id"], r["cancelled"]))
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
                arrival_estimated=bool(r["arrival_inferred"]),
                departure_estimated=bool(r["departure_inferred"]),
            )
        )
    days = [
        schema.HistoryDay(
            service_date=day,
            trip_id=trip,
            cancelled=cancelled,
            worked_by=worked_by.get(day, []),
            calls=calls[day],
        )
        for day, (trip, cancelled) in seen.items()
    ]
    stops = _stops(days)
    segment: schema.HistorySegment | None = None
    if stops:
        ids = [s.location_id for s in stops]
        first = board if board in ids[:-1] else ids[0]
        last = alight if alight in ids[ids.index(first) + 1 :] else ids[-1]
        days = [d.model_copy(update={"segment": run(d, first, last)}) for d in days]
        segment = summary(days, first, last)
    return schema.TripHistory(
        **envelope(snap),
        trip_id=trip_id,
        journey=journey,
        since=start,
        until=end,
        days=days,
        cancelled=sum(d.cancelled for d in days),
        stops=stops,
        segment=segment,
    )


@dataclass(frozen=True, slots=True)
class Assembled:
    """What a trip on a date is shown with, read once for a trip page and an enriched leg."""

    row: DictRow
    journey: schema.JourneyKey | None
    realtime: schema.TripRealtime | None
    vehicles: list[schema.TripVehicle]
    composition: bool
    features: list[schema.Feature]
    calls: list[schema.TripCall]
    alerts: list[schema.Alert]


def assemble(snap: Snapshot, limits: Limits, trip: TripOnDate) -> Assembled:
    """A running trip of the active release as it is shown: its line, realtime, vehicles,
    features, calls and alerts."""

    params = {
        "trip_id": trip.trip_id,
        "service_date": trip.service_date,
        "release_id": snap.release_id,
    }
    row = snap.one("trip", params)
    assert row is not None  # resolved against the same snapshot
    found = snap.one("trip_realtime", params)
    realtime = None if found is None else trip_realtime(found)
    stated = features(
        load_codes(),
        snap.all(
            "trip_features",
            {"trip_id": trip.trip_id, "route_id": row["route_id"], "service_id": row["service_id"]},
        ),
    )
    oldest = seen_since(snap, limits)
    vehicles = snap.all(
        "trip_vehicles",
        params
        | {
            "trip_ids": [trip.trip_id],
            "service_dates": [trip.service_date],
            "seen_since": oldest,
        },
    )
    calls = snap.all("trip_calls", params)
    return Assembled(
        row=row,
        journey=trip.journey,
        realtime=realtime,
        vehicles=[trip_vehicle(v) for v in vehicles],
        composition=can_ask(snap, "composition", trip),
        features=stated,
        calls=[
            trip_call(
                c,
                stated,
                first=n == 0,
                last=n == len(calls) - 1,
                stale=realtime is not None and realtime.stale,
                passed=_passed(
                    c, n == len(calls) - 1, snap.now, timedelta(seconds=limits.passed_after_s)
                ),
            )
            for n, c in enumerate(calls)
        ],
        alerts=trip_alerts(
            find_alerts(
                snap,
                limits,
                routes=[row["route_id"]],
                stops=sorted({c["location_id"] for c in calls}),
                trips=[trip.trip_id],
            ),
            route=row["route_id"],
            trip=trip.trip_id,
        ),
    )


@dataclass(frozen=True, slots=True)
class RunPart:
    """A part of a run as its alerts are told apart: its trip, line and alerts."""

    trip_id: str
    route_id: str
    line: schema.Line
    trip_short_name: str | None
    alerts: list[schema.Alert]


def run_alerts(
    calls: list[schema.RunCall], ahead: int | None, parts: list[RunPart]
) -> list[schema.RunAlert]:
    """The alerts a run shows: one informing a part or its line, with that part's line; and a
    stop's (the severe ones a trip is sent) at the first call still ahead there (from `ahead`,
    not passed), else not at all."""

    routes = {(p.route_id): p for p in parts}
    trips = {p.trip_id: p for p in parts}
    shown: dict[str, schema.RunAlert] = {}
    for part in parts:
        for alert in part.alerts:
            if alert.alert_id in shown:
                continue
            ours = next(
                (
                    trips.get(t.id) if t.kind == "trip" else routes.get(t.id)
                    for t in alert.informs
                    if (t.kind == "trip" and t.id in trips)
                    or (t.kind == "route" and t.id in routes)
                ),
                None,
            )
            if ours is not None:
                shown[alert.alert_id] = schema.RunAlert(
                    alert=alert, call=None, line=ours.line, trip_short_name=ours.trip_short_name
                )
                continue
            stops = {t.id for t in alert.informs if t.kind == "stop"} | {
                t.stop_place for t in alert.informs if t.kind == "stop" and t.stop_place
            }
            at = next(
                (
                    n
                    for n in range(len(calls) if ahead is None else ahead, len(calls))
                    if calls[n].call.location_id in stops and not calls[n].call.passed
                ),
                None,
            )
            if at is not None:
                shown[alert.alert_id] = schema.RunAlert(
                    alert=alert, call=at, line=part.line, trip_short_name=part.trip_short_name
                )
    return list(shown.values())


def _run(
    snap: Snapshot,
    limits: Limits,
    trip: TripOnDate,
    shown: Assembled,
    parts: list[DictRow],
) -> schema.TripRun:
    """The run from this trip on: its calls, then each later part's, a part starting where the
    previous one ends merged into its last call (with the later departure); and the alerts the
    run shows (its parts' own and their lines', and the severe ones of its stops still ahead,
    each at the call it is shown at)."""

    ids = [p["trip_id"] for p in parts]
    later = ids[ids.index(trip.trip_id) + 1 :] if trip.trip_id in ids else []
    pieces: list[tuple[TripOnDate, Assembled]] = [(trip, shown)]
    for trip_id in later:
        part = trip_on(snap, trip_id, trip.service_date)
        if part is None or not part.runs:
            break
        pieces.append((part, assemble(snap, limits, part)))
    calls: list[schema.RunCall] = []
    for n, (part, assembled) in enumerate(pieces):
        own = assembled.calls
        if n > 0 and calls and own and calls[-1].call.location_id == own[0].location_id:
            last, first = calls[-1].call, own[0]
            calls[-1] = calls[-1].model_copy(
                update={
                    "call": last.model_copy(
                        update={
                            "scheduled_departure": first.scheduled_departure,
                            "estimated_departure": first.estimated_departure,
                            "platform": last.platform or first.platform,
                            "passed": first.passed,
                        }
                    )
                }
            )
            own = own[1:]
        changes = line(assembled.row).route_id != line(shown.row).route_id
        calls.extend(
            schema.RunCall(
                trip_id=part.trip_id,
                continues_as=line(assembled.row) if i == 0 and changes else None,
                call=call,
            )
            for i, call in enumerate(own)
        )
    first_ahead = next_call([c.call for c in calls])
    shown_parts = [
        RunPart(p.trip_id, a.row["route_id"], line(a.row), a.row["trip_short_name"], a.alerts)
        for p, a in pieces
    ]
    return schema.TripRun(
        calls=calls, next=first_ahead, alerts=run_alerts(calls, first_ahead, shown_parts)
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
    include: Annotated[
        str | None,
        Query(description="Comma-separated: `shape` adds the GeoJSON path, `run` the whole run"),
    ] = None,
) -> schema.Trip | JSONResponse:
    included = {part.strip() for part in (include or "").split(",") if part.strip()}
    realtime_cache(response, limits)
    found = trip_on(snap, trip_id, service_date)
    if found is None:
        journey = gone(snap, trip_id, service_date)
        if journey is None:
            raise HTTPException(404, f"no trip {trip_id}")
        body = schema.GoneTrip(
            title="Gone",
            status=410,
            detail=f"{trip_id} is from an older timetable version",
            service_date=service_date,
            journey=journey,
            by_key=by_key_path(journey.namespace, journey.key, service_date),
        )
        return JSONResponse(
            body.model_dump(mode="json"), status_code=410, media_type="application/problem+json"
        )
    if not found.runs:
        raise HTTPException(404, f"{trip_id} does not run on {service_date}")
    shown = assemble(snap, limits, found)
    row, journey = shown.row, shown.journey
    params = {"trip_id": trip_id, "service_date": service_date, "release_id": snap.release_id}
    parts = (
        snap.all("trip_parts", {"run_key": row["run_key"], "service_date": service_date})
        if row["run_key"] is not None
        else []
    )
    links = (
        []
        if journey is None
        else snap.all(
            "trip_links",
            {"namespace": journey.namespace, "key": journey.key, "service_date": service_date},
        )
    )
    shape = None
    if "shape" in included and row["shape_id"] is not None:
        geometry = snap.one("trip_shape", {"shape_id": row["shape_id"]})
        shape = None if geometry is None else geometry["geometry"]
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
        amenities=amenities(shown.features),
        realtime=shown.realtime,
        vehicles=shown.vehicles,
        run_parts=[schema.TripPart.model_validate(p) for p in parts]
        or [schema.TripPart(trip_id=trip_id, run_part=None)],
        composition=shown.composition,
        calls=shown.calls,
        next_call=next_call(shown.calls),
        run=_run(snap, limits, found, shown, parts) if "run" in included else None,
        features=shown.features,
        notes=[f.note for f in shown.features if f.note is not None],
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
        alerts=shown.alerts,
        shape=shape,
    )
