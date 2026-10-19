"""The active release and the sources' recent polls, for the frontend's stale-data banner."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Response

from obehy.api import schema
from obehy.api.common import LimitsDep, SnapshotDep, envelope, realtime_cache

router = APIRouter(tags=["status"])


@router.get("/status", response_model=schema.Status, summary="Release and source freshness")
def status(response: Response, snap: SnapshotDep, limits: LimitsDep) -> schema.Status:
    realtime_cache(response, limits)
    rows = snap.all("status_sources", {"since": snap.now - timedelta(hours=1)})
    return schema.Status(
        **envelope(snap),
        api_version=limits.version,
        activated_at=snap.activated_at,
        sources=[schema.SourceHealth.model_validate(r) for r in rows],
    )
