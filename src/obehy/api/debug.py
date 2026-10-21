"""Debugging (BASE_PLAN.md section 30): the active release; each source's polls and what it
contributes now; unbound observations; one journey's state, parts, vehicles and evidence; one
vehicle's identity (reports, keys, pairings, statements, profile); the pairings to review as
registry keys; the registry's datasets and disagreements; alerts with each entity resolved or
not; a trip's raw static codes. Not cached; raw rows, whose shapes follow the tables and may
change."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from psycopg.rows import DictRow

from obehy.api.common import LimitsDep, SnapshotDep, envelope

router = APIRouter(prefix="/debug", tags=["debug"])

Minutes = Annotated[int | None, Query(ge=1, le=24 * 60)]


def _kinds(rows: list[DictRow], *kinds: str) -> dict[str, list[Any]]:
    return {kind: [r["body"] for r in rows if r["row_kind"] == kind] for kind in kinds}


@router.get("/release", summary="The active release, its packages, loads and activations")
def release(snap: SnapshotDep, limits: LimitsDep) -> dict[str, Any]:
    return envelope(snap) | {"release": snap.one("debug_release", {"limit": limits.debug_limit})}


@router.get("/realtime/sources", summary="Polls per source channel, and what each feeds now")
def sources(snap: SnapshotDep, limits: LimitsDep, minutes: Minutes = None) -> dict[str, Any]:
    since = snap.now - timedelta(minutes=minutes or limits.debug_window_min)
    seen_since = snap.now - timedelta(seconds=limits.vehicle_max_age_s)
    return envelope(snap) | {
        "since": since,
        "channels": snap.all("debug_sources", {"since": since}),
        "current": snap.all(
            "debug_source_current", {"release_id": snap.release_id, "since": seen_since}
        ),
    }


@router.get("/realtime/unmatched", summary="Recent observations the core did not bind")
def unmatched(snap: SnapshotDep, limits: LimitsDep, minutes: Minutes = None) -> dict[str, Any]:
    since = snap.now - timedelta(minutes=minutes or limits.debug_window_min)
    rows = snap.all("debug_unmatched", {"since": since, "limit": limits.debug_limit})
    return envelope(snap) | {"since": since, "groups": rows}


@router.get(
    "/realtime/instances/{feed}/{namespace}/{key}/{service_date}",
    summary="A journey's state, parts, vehicle reports and recent observations",
)
def instance(
    snap: SnapshotDep,
    limits: LimitsDep,
    feed: str,
    namespace: str,
    key: str,
    service_date: date,
    minutes: Minutes = None,
) -> dict[str, Any]:
    since = snap.now - timedelta(minutes=minutes or limits.debug_window_min)
    rows = snap.all(
        "debug_instance",
        {
            "feed": feed,
            "namespace": namespace,
            "key": key,
            "service_date": service_date,
            "since": since,
            "limit": limits.debug_limit,
        },
    )
    found = _kinds(rows, "state", "part", "vehicle", "observation")
    return envelope(snap) | {
        "state": found["state"][0] if found["state"] else None,
        "parts": found["part"],
        "vehicles": found["vehicle"],
        "observations": found["observation"],
    }


@router.get("/alerts", summary="Alerts with each entity and what it resolved to")
def alerts(snap: SnapshotDep, limits: LimitsDep, minutes: Minutes = None) -> dict[str, Any]:
    since = snap.now - timedelta(minutes=minutes or limits.debug_window_min)
    rows = snap.all("debug_alerts", {"since": since, "limit": limits.alerts_limit})
    return envelope(snap) | {"since": since, "alerts": rows}


@router.get("/lookups", summary="Lookup requests and the upstream requests they caused")
def lookups(snap: SnapshotDep, limits: LimitsDep, minutes: Minutes = None) -> dict[str, Any]:
    since = snap.now - timedelta(minutes=minutes or limits.debug_window_min)
    rows = snap.all("debug_lookups", {"since": since, "limit": limits.debug_limit})
    return envelope(snap) | {"since": since, "lookups": rows}


@router.get("/vehicles/pairings", summary="Reports paired by running side by side")
def pairings(
    snap: SnapshotDep,
    limits: LimitsDep,
    confirmed: Annotated[
        bool | None, Query(description="Only confirmed or only candidates")
    ] = None,
) -> dict[str, Any]:
    rows = snap.all("debug_pairings", {"confirmed": confirmed, "limit": limits.debug_limit})
    return envelope(snap) | {"pairings": rows}


@router.get("/vehicles/registry", summary="The registry's datasets and the profiles' conflicts")
def registry(snap: SnapshotDep, limits: LimitsDep) -> dict[str, Any]:
    rows = snap.all("debug_registry", {"limit": limits.debug_limit})
    found = _kinds(rows, "dataset", "profiles", "conflict")
    return envelope(snap) | {
        "datasets": found["dataset"],
        "profiles": found["profiles"][0],
        "conflicts": found["conflict"],
    }


@router.get(
    "/vehicles/{vehicle_id}",
    summary="Why a vehicle is what it is: reports, keys, pairings, statements, profile",
)
def vehicle(snap: SnapshotDep, vehicle_id: str) -> dict[str, Any]:
    rows = snap.all("debug_vehicle", {"vehicle_id": vehicle_id})
    if not rows:
        raise HTTPException(404, f"no vehicle {vehicle_id}")
    found = _kinds(rows, "report", "key", "pairing", "observed", "curated", "profile")
    return envelope(snap) | {
        "vehicle_id": vehicle_id,
        "reports": found["report"],
        "keys": found["key"],
        "pairings": found["pairing"],
        "observed": found["observed"],
        "curated": found["curated"],
        "profile": found["profile"][0] if found["profile"] else None,
    }


@router.get("/static/trips/{trip_id}", summary="A trip's raw assignment rows")
def trip_codes(snap: SnapshotDep, trip_id: str) -> dict[str, Any]:
    row = snap.one("trip", {"trip_id": trip_id, "service_date": date.min})
    if row is None:
        raise HTTPException(404, f"no trip {trip_id}")
    rows = snap.all(
        "debug_assignments",
        {"trip_id": trip_id, "route_id": row["route_id"], "service_id": row["service_id"]},
    )
    return envelope(snap) | {"trip_id": trip_id, "assignments": rows}
