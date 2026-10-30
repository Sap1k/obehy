"""The FastAPI application: routers under `/api/v1`, problem+json errors, a read-only pool and a
small one for lookup requests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from starlette.exceptions import HTTPException

from obehy.api import (
    alerts,
    boards,
    circulations,
    compositions,
    debug,
    lines,
    schema,
    status,
    stops,
    trips,
    vehicles,
)
from obehy.api.db import NoActiveRelease, open_pool, open_request_pool
from obehy.api.limits import Limits, load_limits
from obehy.api.params import ParameterError

PREFIX = "/api/v1"


def _problem(status_code: int, title: str, detail: str | None) -> JSONResponse:
    body = schema.Problem(title=title, status=status_code, detail=detail)
    return JSONResponse(
        body.model_dump(mode="json"), status_code=status_code, media_type="application/problem+json"
    )


async def _http_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, HTTPException)
    titles = {404: "Not Found", 409: "Conflict", 503: "Service Unavailable"}
    return _problem(error.status_code, titles.get(error.status_code, "Error"), error.detail)


async def _parameter_error(_: Request, error: Exception) -> JSONResponse:
    return _problem(422, "Invalid parameter", str(error))


async def _validation_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors())
    return _problem(422, "Invalid parameter", detail)


async def _no_release(_: Request, error: Exception) -> JSONResponse:
    return _problem(503, "Service Unavailable", str(error))


def create_app(
    database_url: str | None = None,
    *,
    pool: ConnectionPool[Any] | None = None,
    limits: Limits | None = None,
) -> FastAPI:
    """The app over `pool`, or over a pool it opens on `database_url` for its lifetime."""

    if pool is None and database_url is None:
        raise ValueError("create_app needs a database URL or a pool")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        owned = pool is None
        app.state.pool = pool if pool is not None else open_pool(str(database_url))
        app.state.limits = limits or load_limits()
        app.state.requests = open_request_pool(str(database_url or app.state.pool.conninfo))
        try:
            yield
        finally:
            app.state.requests.close()
            if owned:
                app.state.pool.close()

    app = FastAPI(
        title="Oběhy",
        version="1",
        summary="Czech public transport: stops, boards, trips, lines and vehicles, live.",
        openapi_url=f"{PREFIX}/openapi.json",
        docs_url=f"{PREFIX}/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(ParameterError, _parameter_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(NoActiveRelease, _no_release)
    for module in (
        status,
        stops,
        boards,
        trips,
        compositions,
        circulations,
        lines,
        vehicles,
        alerts,
        debug,
    ):
        app.include_router(module.router, prefix=PREFIX)
    return app
