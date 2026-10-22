"""Alerts (BASE_PLAN.md section 25): from any source, at the scope the source gave, resolved in
the active release (`rt.alert_target`). Listed on their own and attached to stop places,
lines, trips and boards. Never broadened: a line's alert is the line's, a stop's the stop's."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response

from obehy.api import schema
from obehy.api.codes import load_codes
from obehy.api.common import LimitsDep, SnapshotDep, envelope, realtime_cache
from obehy.api.db import Snapshot
from obehy.api.limits import Limits

router = APIRouter(tags=["alerts"])


def find_alerts(
    snap: Snapshot,
    limits: Limits,
    *,
    routes: Sequence[str] = (),
    stops: Sequence[str] = (),
    trips: Sequence[str] = (),
    everything: bool = False,
    alert_id: str | None = None,
    withdrawn: bool = False,
    over: bool = False,
    at: datetime | None = None,
) -> list[schema.Alert]:
    """The alerts informing any of `routes`, stop places `stops` (or their boarding points) or
    `trips`, current at `at` (default: now); with `withdrawn` and `over`, also those."""

    source, _, own_id = (alert_id or "").partition(":")
    rows = snap.all(
        "alerts",
        {
            "routes": list(routes),
            "stops": list(stops),
            "trips": list(trips),
            "everything": everything,
            "source": source or None,
            "alert_id": own_id or None,
            "withdrawn": withdrawn,
            "over": over,
            "at": at or snap.now,
            "limit": limits.alerts_limit,
        },
    )
    codes = load_codes()
    return [
        schema.Alert.model_validate(
            r
            | {
                "alert_id": f"{r['source']}:{r['alert_id']}",
                "source_label": codes.source(r["source"]),
            }
        )
        for r in rows
    ]


def alert_ids(alerts: Sequence[schema.Alert], *, route: str, stop: str, trip: str) -> list[str]:
    """The alerts informing a board row: its line, its stop place or its trip."""

    def informs(target: schema.AlertTarget) -> bool:
        return (
            (target.kind == "route" and target.id == route)
            or (target.kind == "stop" and stop in (target.id, target.stop_place))
            or (target.kind == "trip" and target.id == trip)
        )

    return [a.alert_id for a in alerts if any(informs(t) for t in a.informs)]


def trip_alerts(alerts: Sequence[schema.Alert], *, route: str, trip: str) -> list[schema.Alert]:
    """A trip's alerts among those of its line, its stops and itself: its own and its line's,
    and of its stops' only the severe ones (a stop's others belong on that stop's board)."""

    def own(target: schema.AlertTarget) -> bool:
        return (target.kind == "route" and target.id == route) or (
            target.kind == "trip" and target.id == trip
        )

    return [a for a in alerts if a.severity == "SEVERE" or any(own(t) for t in a.informs)]


@router.get("/alerts", response_model=schema.Alerts, summary="Current alerts, by scope")
def alerts(
    response: Response,
    snap: SnapshotDep,
    limits: LimitsDep,
    line: Annotated[str | None, Query(description="A route_id")] = None,
    stop: Annotated[str | None, Query(description="A stop place (and its boarding points)")] = None,
    trip: Annotated[str | None, Query(description="A trip_id")] = None,
    at: Annotated[datetime | None, Query(description="Current at; default now")] = None,
) -> schema.Alerts:
    realtime_cache(response, limits)
    scoped: dict[str, Any] = {
        "routes": [line] if line else [],
        "stops": [stop] if stop else [],
        "trips": [trip] if trip else [],
    }
    found = find_alerts(snap, limits, **scoped, everything=not (line or stop or trip), at=at)
    return schema.Alerts(**envelope(snap), at=at or snap.now, alerts=found)


@router.get(
    "/alerts/{alert_id}",
    response_model=schema.Alert,
    responses={404: {"model": schema.Problem}},
    summary="One alert, also once withdrawn or over",
)
def alert(response: Response, snap: SnapshotDep, limits: LimitsDep, alert_id: str) -> schema.Alert:
    realtime_cache(response, limits)
    found = find_alerts(snap, limits, alert_id=alert_id, withdrawn=True, over=True)
    if not found:
        raise HTTPException(404, f"no alert {alert_id}")
    return found[0]
