"""Shared request dependencies, caching headers and row shaping."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, cast

from fastapi import Depends, Request, Response
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from obehy.api import schema
from obehy.api.db import Snapshot, snapshot
from obehy.api.limits import Limits
from obehy.realtime.times import PRAGUE


def _snapshot(request: Request) -> Iterator[Snapshot]:
    yield from snapshot(cast(ConnectionPool[Any], request.app.state.pool))


def _limits(request: Request) -> Limits:
    return cast(Limits, request.app.state.limits)


SnapshotDep = Annotated[Snapshot, Depends(_snapshot)]
LimitsDep = Annotated[Limits, Depends(_limits)]


def envelope(snap: Snapshot) -> dict[str, Any]:
    return {"release_id": snap.release_id, "generated_at": snap.now.astimezone(PRAGUE)}


def realtime_cache(response: Response, limits: Limits) -> None:
    response.headers["Cache-Control"] = f"public, max-age={limits.cache_realtime_s}"


def static_cache(
    request: Request, response: Response, snap: Snapshot, limits: Limits
) -> Response | None:
    """Static answers change only with the release: it is their ETag. A 304 when it matches."""

    etag = f'"{snap.release_id}"'
    headers = {"ETag": etag, "Cache-Control": f"public, max-age={limits.cache_static_s}"}
    response.headers.update(headers)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return None


def line(row: DictRow) -> schema.Line:
    return schema.Line(
        route_id=row["route_id"],
        short_name=row["short_name"],
        long_name=row["long_name"],
        mode=row["mode"],
        color=row["color"],
        text_color=row["text_color"],
    )
