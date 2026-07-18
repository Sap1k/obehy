from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from obehy.persistence.database import create_database_engine


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.environ.get("OBEHY_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("OBEHY_TEST_DATABASE_URL is not set")
    return value


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    instance = create_database_engine(database_url)
    with instance.connect() as connection:
        connection.execute(text("SELECT 1 FROM canonical_entity LIMIT 1"))
    yield instance
    instance.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
