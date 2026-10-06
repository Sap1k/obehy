"""Raw-SQL migrations, applied in order and recorded with their checksums."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
# Session advisory lock so two processes never migrate at once.
_LOCK = 0x6F62_6D69


class MigrationError(RuntimeError):
    """The migration set is inconsistent with the database."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    sha256: str


def discover(directory: Path = MIGRATIONS) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if match is None:
            raise MigrationError(f"Unexpected migration file name: {path.name}")
        data = path.read_bytes()
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=data.decode("utf-8"),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"Migration versions must be 1..n without gaps: {versions}")
    return migrations


def pending(applied: dict[int, str], migrations: list[Migration]) -> list[Migration]:
    """Migrations still to apply; an applied file whose checksum changed is an error."""

    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.sha256:
            raise MigrationError(
                f"Migration {migration.version:04d}_{migration.name} changed after it was "
                "applied; add a new migration instead"
            )
    unknown = sorted(set(applied) - {migration.version for migration in migrations})
    if unknown:
        raise MigrationError(f"Database has migrations this checkout does not know: {unknown}")
    return [migration for migration in migrations if migration.version not in applied]


def migrate(connection: psycopg.Connection, directory: Path = MIGRATIONS) -> list[Migration]:
    """Apply pending migrations, each in its own transaction. Needs an autocommit connection."""

    migrations = discover(directory)
    connection.execute("SELECT pg_advisory_lock(%s)", (_LOCK,))
    try:
        with connection.transaction():
            connection.execute("CREATE SCHEMA IF NOT EXISTS control")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS control.schema_migration ("
                " version integer PRIMARY KEY,"
                " name text NOT NULL,"
                " sha256 text NOT NULL,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
        applied = dict(
            connection.execute("SELECT version, sha256 FROM control.schema_migration").fetchall()
        )
        todo = pending(applied, migrations)
        for migration in todo:
            with connection.transaction():
                connection.execute(migration.sql.encode("utf-8"))
                connection.execute(
                    "INSERT INTO control.schema_migration (version, name, sha256)"
                    " VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.sha256),
                )
        return todo
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (_LOCK,))
