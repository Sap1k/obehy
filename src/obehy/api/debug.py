"""Debugging the realtime pipeline (BASE_PLAN.md section 30): source health, unbound
observations, one journey's state and evidence, a trip's raw static codes. Not cached, not in
the public schema; shapes may change."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from obehy.api.common import LimitsDep, SnapshotDep, envelope

router = APIRouter(prefix="/debug", tags=["debug"], include_in_schema=False)

Minutes = Annotated[int | None, Query(ge=1, le=24 * 60)]


@router.get("/realtime/sources")
def sources(snap: SnapshotDep, limits: LimitsDep, minutes: Minutes = None) -> dict[str, Any]:
    since = snap.now - timedelta(minutes=minutes or limits.debug_window_min)
    return envelope(snap) | {
        "since": since,
        "channels": snap.all("debug_sources", {"since": since}),
    }


@router.get("/realtime/unmatched")
def unmatched(snap: SnapshotDep, limits: LimitsDep, minutes: Minutes = None) -> dict[str, Any]:
    since = snap.now - timedelta(minutes=minutes or limits.debug_window_min)
    rows = snap.all("debug_unmatched", {"since": since, "limit": limits.debug_limit})
    return envelope(snap) | {"since": since, "groups": rows}


@router.get("/realtime/instances/{feed}/{namespace}/{key}/{service_date}")
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
    state = [r["body"] for r in rows if r["row_kind"] == "state"]
    return envelope(snap) | {
        "state": state[0] if state else None,
        "parts": [r["body"] for r in rows if r["row_kind"] == "part"],
        "observations": [
            r["body"] | {"received_at": r["at"]} for r in rows if r["row_kind"] == "observation"
        ],
    }


@router.get("/static/trips/{trip_id}")
def trip_codes(snap: SnapshotDep, trip_id: str) -> dict[str, Any]:
    row = snap.one("trip", {"trip_id": trip_id, "service_date": date.min})
    if row is None:
        raise HTTPException(404, f"no trip {trip_id}")
    rows = snap.all(
        "debug_assignments",
        {"trip_id": trip_id, "route_id": row["route_id"], "service_id": row["service_id"]},
    )
    return envelope(snap) | {"trip_id": trip_id, "assignments": rows}
