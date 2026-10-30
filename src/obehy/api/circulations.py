"""Observed circulations (oběhy, BASE_PLAN.md section 22): the journeys each vehicle worked in a
row, cut by the nightly job's rule (`history.vehicle_chains`) and read live from the vehicle
assignments, so today's are there too. A vehicle's days; a trip's oběh on a date, with the
feed's own block where it has one. Learned and roster tours are not served yet.

History knows a vehicle by its source's ID; which registry vehicle that was on a date is
`history.vehicle_of` (by the registry's source-ID keys).
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.rows import DictRow

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, line, realtime_cache
from obehy.api.db import Snapshot
from obehy.api.params import date_span
from obehy.realtime.policy import load_policy
from obehy.realtime.times import local_date

router = APIRouter()


@cache
def _gap_s() -> int:
    return load_policy().time.vehicle_day_gap_s


def _chains(
    snap: Snapshot,
    sources: list[DictRow],
    since: date,
    until: date,
    vehicle_id: str | None = None,
) -> list[tuple[date, schema.VehicleChain]]:
    """The chains of `sources` dated `since` to `until` (`vehicle_chains.sql`), latest day
    first; only `vehicle_id`'s when given."""

    if not sources:
        return []
    params: dict[str, Any] = {
        "since": since,
        "until": until,
        "gap_s": _gap_s(),
        "sources": [s["source"] for s in sources],
        "source_vehicle_ids": [s["source_vehicle_id"] for s in sources],
        "vehicle_id": vehicle_id,
    }
    found: dict[tuple[Any, ...], tuple[date, schema.VehicleChain]] = {}
    for r in snap.all("vehicle_chains", params):
        key = (r["day"], r["vehicle_id"], r["feed"], r["source"], r["source_vehicle_id"], r["seq"])
        if key not in found:
            found[key] = (
                r["day"],
                schema.VehicleChain(
                    vehicle_id=r["vehicle_id"],
                    source=r["source"],
                    source_vehicle_id=r["source_vehicle_id"],
                    first_seen=r["chain_first_seen"],
                    last_seen=r["chain_last_seen"],
                    journeys=[],
                ),
            )
        journeys = found[key][1].journeys
        before = journeys[-1].last_seen if journeys else None
        journeys.append(
            schema.ChainJourney(
                journey=schema.JourneyKey(namespace=r["key_namespace"], key=r["key"]),
                service_date=r["service_date"],
                trip_id=r["trip_id"],
                line=line(r) if r["route_id"] is not None else None,
                route_name=r["route_name"],
                headsign=r["headsign"],
                origin=r["origin"],
                departure=r["departure"],
                destination=r["destination"],
                arrival=r["arrival"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                layover_s=None
                if before is None
                else max(0, round((r["first_seen"] - before).total_seconds())),
            )
        )
    return list(found.values())


@router.get(
    "/vehicles/{vehicle_id}/days",
    tags=["vehicles"],
    response_model=schema.VehicleDays,
    summary="A vehicle's oběhy: the journeys it worked, day by day",
)
def vehicle_days(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    vehicle_id: str,
    since: Annotated[date | None, Query(description="First service date")] = None,
    until: Annotated[date | None, Query(description="Last service date; default today")] = None,
) -> schema.VehicleDays:
    realtime_cache(response, limits)
    start, end = date_span(
        since,
        until,
        local_date(snap.now),
        limits.history_default_days,
        limits.history_max_days,
    )
    sources = snap.all("vehicle_sources", {"vehicle_id": vehicle_id})
    days: dict[date, list[schema.VehicleChain]] = {}
    for day, chain in _chains(snap, sources, start, end, vehicle_id):
        days.setdefault(day, []).append(chain)
    return schema.VehicleDays(
        **envelope(snap),
        vehicle_id=vehicle_id,
        since=start,
        until=end,
        days=[schema.VehicleDay(service_date=day, chains=chains) for day, chains in days.items()],
    )


@router.get(
    "/trips/{trip_id}/{service_date}/circulation",
    tags=["trips"],
    response_model=schema.Circulation,
    responses={404: {"model": schema.Problem}},
    summary="A trip's oběh on a date: the vehicles that worked it and their day, and the plan",
)
def trip_circulation(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    trip_id: str,
    service_date: date,
) -> schema.Circulation:
    realtime_cache(response, limits)
    row = snap.one(
        "trip", {"trip_id": trip_id, "service_date": service_date, "release_id": snap.release_id}
    )
    if row is None or not row["runs"]:
        raise HTTPException(404, f"no trip {trip_id} runs on {service_date}")
    keys = snap.all("trip_keys", {"trip_id": trip_id, "service_date": service_date})
    wanted = {(k["namespace"], k["key"]) for k in keys}
    sources = snap.all(
        "journey_sources",
        {
            "service_date": service_date,
            "namespaces": [k["namespace"] for k in keys],
            "keys": [k["key"] for k in keys],
        },
    )
    # A chain is dated by its first journey: one past midnight may start the day before.
    observed: list[schema.ObservedCirculation] = []
    for _, chain in _chains(snap, sources, service_date - timedelta(days=1), service_date):
        position = next(
            (
                n
                for n, j in enumerate(chain.journeys)
                if j.service_date == service_date and (j.journey.namespace, j.journey.key) in wanted
            ),
            None,
        )
        if position is not None:
            observed.append(schema.ObservedCirculation(**chain.model_dump(), position=position))
    block = snap.all("trip_block", {"trip_id": trip_id, "service_date": service_date})
    planned = (
        schema.PlannedCirculation(
            source="block",
            trips=[
                schema.BlockTrip(
                    trip_id=b["trip_id"],
                    service_date=service_date,
                    line=line(b),
                    trip_short_name=b["trip_short_name"],
                    headsign=b["headsign"],
                    origin=b["origin"],
                    departure=b["departure"],
                    destination=b["destination"],
                    arrival=b["arrival"],
                )
                for b in block
            ],
        )
        if len(block) > 1
        else None
    )
    return schema.Circulation(
        **envelope(snap),
        trip_id=trip_id,
        service_date=service_date,
        observed=observed,
        planned=planned,
    )
