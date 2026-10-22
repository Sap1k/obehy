"""The active release and how live its realtime is, for the frontend's data status: each
source's newest vehicle report (the worker writes them every tick), and its recent polls."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Response

from obehy.api import schema
from obehy.api.codes import load_codes
from obehy.api.common import LimitsDep, SnapshotDep, envelope, realtime_cache

router = APIRouter(tags=["status"])


@router.get("/status", response_model=schema.Status, summary="Release and source freshness")
def status(response: Response, snap: SnapshotDep, limits: LimitsDep) -> schema.Status:
    realtime_cache(response, limits)
    rows = snap.all("status_sources", {"since": snap.now - timedelta(hours=1)})
    codes = load_codes()
    realtime = [
        schema.RealtimeFreshness.model_validate(r | {"label": codes.source(r["source"])})
        for r in snap.all("status_realtime", {"release_id": snap.release_id})
    ]
    return schema.Status(
        **envelope(snap),
        api_version=limits.version,
        activated_at=snap.activated_at,
        sources=[schema.SourceHealth.model_validate(r) for r in rows],
        realtime=realtime,
        realtime_at=max((r.last_seen for r in realtime), default=None),
    )
