"""PostgreSQL fixtures: one migrated template per session, one fresh database per test.

Set ``OBEHY_TEST_DATABASE_URL`` to a database the user may create databases from; the tests
are skipped without it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import conninfo, sql

from obehy.release.migrate import migrate

TEST_DATABASE_URL_ENV = "OBEHY_TEST_DATABASE_URL"


def _url(base: str, database: str) -> str:
    return conninfo.make_conninfo(base, dbname=database)


def _create(base: str, database: str, template: str | None = None) -> None:
    with psycopg.connect(base, autocommit=True) as admin:
        statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
        if template is not None:
            statement = sql.SQL("{} TEMPLATE {}").format(statement, sql.Identifier(template))
        admin.execute(statement)


def _drop(base: str, database: str) -> None:
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} is not set")
    return url


@pytest.fixture(scope="session")
def template_database(admin_url: str) -> Iterator[str]:
    name = f"obehy_tmpl_{uuid.uuid4().hex[:12]}"
    _create(admin_url, name)
    try:
        with psycopg.connect(_url(admin_url, name), autocommit=True) as connection:
            migrate(connection)
        yield name
    finally:
        _drop(admin_url, name)


@pytest.fixture
def database_url(admin_url: str, template_database: str) -> Iterator[str]:
    name = f"obehy_t_{uuid.uuid4().hex[:12]}"
    _create(admin_url, name, template_database)
    try:
        yield _url(admin_url, name)
    finally:
        _drop(admin_url, name)


@pytest.fixture
def connection(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=True) as value:
        yield value
