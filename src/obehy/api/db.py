"""Database access: one read-only, repeatable-read transaction per request.

The request reads the active publication first and every later query in the same snapshot, so
the static rows (`active.*`) and the realtime rows (filtered by that `release_id`) belong to one
release. Right after an activation a trip shows as scheduled until the worker re-emits; it never
shows another release's state.

Queries live in `sql/*.sql`, one per file; Python binds parameters and shapes rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from obehy.realtime.times import Instant, instant

SQL = Path(__file__).resolve().parent / "sql"
PUBLICATION: LiteralString = (
    "SELECT run_id, activated_at, statement_timestamp() AS now FROM control.publication"
)


class NoActiveRelease(RuntimeError):
    """No release is active yet."""


@cache
def query(name: str) -> LiteralString:
    """The text of `sql/<name>.sql`, a trusted query shipped with the package."""

    return cast(LiteralString, (SQL / f"{name}.sql").read_text(encoding="utf-8"))


def _configure(connection: psycopg.Connection[DictRow]) -> None:
    connection.autocommit = True
    # SQL works in UTC, like the instants the worker stores (in columns and in jsonb); local
    # time in SQL is always explicit (`AT TIME ZONE 'Europe/Prague'`, `obehy_instant`). The
    # response models answer in Prague time (`schema.Time`).
    connection.execute("SET TIME ZONE 'UTC'")
    connection.read_only = True
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    connection.autocommit = False


def open_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool[Any]:
    pool: ConnectionPool[Any] = ConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        configure=_configure,
        open=False,
        name="obehy-api",
    )
    pool.open(wait=True, timeout=30)
    return pool


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One request's view: the active release and a connection inside its transaction."""

    connection: psycopg.Connection[DictRow]
    release_id: str
    activated_at: datetime
    now: Instant

    def all(self, name: str, params: dict[str, Any] | None = None) -> list[DictRow]:
        return self.connection.execute(query(name), params).fetchall()

    def one(self, name: str, params: dict[str, Any] | None = None) -> DictRow | None:
        return self.connection.execute(query(name), params).fetchone()


def snapshot(pool: ConnectionPool[Any]) -> Iterator[Snapshot]:
    with pool.connection() as connection:
        row = cast(psycopg.Connection[DictRow], connection).execute(PUBLICATION).fetchone()
        if row is None or row["run_id"] is None:
            raise NoActiveRelease("no release is active")
        yield Snapshot(connection, row["run_id"], row["activated_at"], instant(row["now"]))
