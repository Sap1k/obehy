"""``obehy release load``: verify a release directory and load its packages into ``static``.

Each package load gets a ``control.load`` row whose ``load_id`` is the partition key. All
tables of a load are built as standalone tables, checked, and attached in one transaction,
so a failed load leaves no partition behind.
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq
from psycopg import sql
from psycopg.types.json import Jsonb

from obehy.pipeline.files import file_digest
from obehy.production_package import ProductionPackageError, read_manifest
from obehy.release.contract import Contract, ContractError, Relation, check_declared_schema
from obehy.release.ddl import INDEXES, Table, primary_key_columns, static_tables

PACKAGES = ("jdf", "czptt")
BATCH_ROWS = 65_536
# Session advisory lock: one loader at a time, so a 'loading' row seen under it is stale.
_LOCK = 0x6F62_6C64
_CSV = pa_csv.WriteOptions(include_header=False, quoting_style="all_valid")

Report = Callable[[str], None]


class LoadError(RuntimeError):
    """A release or package cannot be loaded."""


@dataclass(frozen=True)
class VerifiedPackage:
    run_id: str
    package: str
    root: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    package_sha256: str
    relation_paths: dict[str, Path]
    row_counts: dict[str, int]


@dataclass(frozen=True)
class LoadResult:
    package: str
    load_id: int | None
    skipped: bool
    seconds: float


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LoadError(f"Cannot read {path}") from error
    if not isinstance(value, dict):
        raise LoadError(f"{path} is not a JSON object")
    return cast(dict[str, Any], value)


def read_release(release_dir: Path) -> dict[str, Any]:
    release = _read_json(release_dir / "release.json")
    if release.get("schema_version") != 1 or not isinstance(release.get("run_id"), str):
        raise LoadError(f"Unsupported release.json in {release_dir}")
    return release


def verify_package(
    release_dir: Path, release: dict[str, Any], package: str, contract: Contract
) -> VerifiedPackage:
    """Check a package against release.json, its manifest hashes and the contract."""

    entry = cast(dict[str, Any] | None, release.get("packages", {}).get(package))
    if entry is None:
        raise LoadError(f"release.json lists no {package} package")
    root = release_dir / package
    try:
        manifest = read_manifest(root, require_publication=True)
    except ProductionPackageError as error:
        raise LoadError(f"{package}: {error}") from error
    manifest_sha256 = file_digest(root / "manifest.json")
    if manifest_sha256 != entry.get("manifest_sha256"):
        raise LoadError(f"{package}: manifest.json does not match release.json")
    try:
        check_declared_schema(contract, manifest["relations"])
    except ContractError as error:
        raise LoadError(f"{package}: {error}") from error

    files = {file["path"]: file for file in cast(list[dict[str, Any]], manifest["files"])}
    declared = {
        relation["name"]: relation for relation in cast(list[dict[str, Any]], manifest["relations"])
    }
    relation_paths: dict[str, Path] = {}
    row_counts: dict[str, int] = {}
    for relation in contract.relations:
        declared_relation = declared[relation.name]
        relative = cast(str, declared_relation["path"])
        file = files.get(relative)
        path = root / relative
        if file is None:
            raise LoadError(f"{package}: manifest lists no file entry for {relative}")
        if not path.is_file() or path.stat().st_size != file["size_bytes"]:
            raise LoadError(f"{package}: {relative} is missing or has the wrong size")
        if file_digest(path) != file["sha256"]:
            raise LoadError(f"{package}: {relative} does not match its manifest hash")
        relation_paths[relation.name] = path
        row_counts[relation.name] = int(declared_relation["row_count"])
    return VerifiedPackage(
        run_id=cast(str, release["run_id"]),
        package=package,
        root=root,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        package_sha256=cast(str, entry.get("package_sha256", "")),
        relation_paths=relation_paths,
        row_counts=row_counts,
    )


def partition_name(table: str, load_id: int) -> sql.Identifier:
    return sql.Identifier("static", f"{table}_l{load_id}")


def _parent(table: str) -> sql.Identifier:
    return sql.Identifier("static", table)


def _columns(names: Sequence[str]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(name) for name in names)


def _create_partition_table(connection: psycopg.Connection, table: Table, load_id: int) -> None:
    target = partition_name(table.name, load_id)
    connection.execute(
        sql.SQL(
            "CREATE TABLE {} (LIKE {} INCLUDING DEFAULTS INCLUDING GENERATED, CHECK (load_id = {}))"
        ).format(target, _parent(table.name), sql.Literal(load_id))
    )
    connection.execute(
        sql.SQL("ALTER TABLE {} ALTER COLUMN load_id SET DEFAULT {}").format(
            target, sql.Literal(load_id)
        )
    )


def _copy_relation(
    connection: psycopg.Connection, relation: Relation, path: Path, load_id: int
) -> int:
    """Stream one Parquet file into its standalone table as CSV; returns the server row count."""

    names = list(relation.column_names)
    statement = sql.SQL("COPY {} ({}) FROM STDIN (FORMAT csv)").format(
        partition_name(relation.name, load_id), _columns(names)
    )
    parquet = pq.ParquetFile(path)
    with connection.cursor() as cursor:
        with cursor.copy(statement) as copy:
            batches: Iterator[pa.RecordBatch] = parquet.iter_batches(  # pyright: ignore[reportUnknownMemberType]
                batch_size=BATCH_ROWS, columns=names
            )
            for batch in batches:
                buffer = io.BytesIO()
                pa_csv.write_csv(batch, buffer, _CSV)
                copy.write(buffer.getvalue())
        return cursor.rowcount


def _build_indexes(connection: psycopg.Connection, table: Table, load_id: int) -> None:
    """Indexes matching the parent's, so ATTACH adopts them instead of building new ones."""

    target = partition_name(table.name, load_id)
    connection.execute(
        sql.SQL("ALTER TABLE {} ADD PRIMARY KEY ({})").format(
            target, _columns(primary_key_columns(table))
        )
    )
    for index in INDEXES:
        if index.table == table.name:
            connection.execute(
                sql.SQL("CREATE INDEX ON {} USING {} ({})").format(
                    target, sql.SQL(index.method), _columns(index.columns)
                )
            )
    connection.execute(sql.SQL("ANALYZE {}").format(target))


def foreign_key_violations(
    connection: psycopg.Connection, contract: Contract, load_id: int
) -> list[str]:
    """Contract foreign keys checked set-wise inside one load (MATCH SIMPLE)."""

    problems: list[str] = []
    for relation in contract.relations:
        for key in relation.foreign_keys:
            not_null = sql.SQL(" AND ").join(
                sql.SQL("s.{} IS NOT NULL").format(sql.Identifier(name)) for name in key.fields
            )
            join = sql.SQL(" AND ").join(
                sql.SQL("t.{} = s.{}").format(sql.Identifier(target), sql.Identifier(source))
                for source, target in zip(key.fields, key.target_fields, strict=True)
            )
            statement = sql.SQL(
                "SELECT count(*), (array_agg(ROW({})::text))[1:3] FROM {} s "
                "WHERE {} AND NOT EXISTS (SELECT 1 FROM {} t WHERE {})"
            ).format(
                sql.SQL(", ").join(sql.SQL("s.{}").format(sql.Identifier(n)) for n in key.fields),
                partition_name(relation.name, load_id),
                not_null,
                partition_name(key.relation, load_id),
                join,
            )
            count, examples = cast(
                tuple[int, list[str] | None], connection.execute(statement).fetchone()
            )
            if count:
                problems.append(
                    f"{relation.name}({', '.join(key.fields)}) -> {key.relation}: "
                    f"{count} missing, e.g. {', '.join(examples or [])}"
                )
    return problems


def unknown_enum_values(
    connection: psycopg.Connection, contract: Contract, load_id: int
) -> dict[str, dict[str, int]]:
    """Enumeration values the contract does not list; later minors may add values."""

    warnings: dict[str, dict[str, int]] = {}
    for relation in contract.relations:
        for field in relation.fields:
            if field.enum is None:
                continue
            allowed = sorted(contract.enumerations[field.enum])
            rows = connection.execute(
                sql.SQL(
                    "SELECT {col}::text, count(*) FROM {table} "
                    "WHERE {col} IS NOT NULL AND NOT ({col}::text = ANY(%s)) GROUP BY 1"
                ).format(
                    col=sql.Identifier(field.name),
                    table=partition_name(relation.name, load_id),
                ),
                (allowed,),
            ).fetchall()
            if rows:
                warnings[f"{relation.name}.{field.name}"] = {
                    cast(str, value): cast(int, count) for value, count in rows
                }
    return warnings


def _derive(connection: psycopg.Connection, load_id: int) -> None:
    def name(table: str) -> sql.Identifier:
        return partition_name(table, load_id)

    # Bit 0 of weekday_mask is Monday; ISO day of week 1 is Monday.
    connection.execute(
        sql.SQL(
            "INSERT INTO {service_date} (service_id, service_date) "
            "SELECT c.service_id, d::date FROM {calendar} c "
            "CROSS JOIN LATERAL generate_series(c.valid_from, c.valid_to, interval '1 day') d "
            "WHERE c.weekday_mask & (1 << (extract(isodow FROM d)::int - 1)) <> 0 "
            "AND NOT EXISTS (SELECT 1 FROM {exception} e WHERE e.service_id = c.service_id "
            "AND e.service_date = d::date AND NOT e.added) "
            "UNION "
            "SELECT service_id, service_date FROM {exception} WHERE added"
        ).format(
            service_date=name("service_date"),
            calendar=name("service_calendar"),
            exception=name("service_exception"),
        )
    )
    connection.execute(
        sql.SQL(
            "INSERT INTO {shape_line} (shape_id, geom) "
            "SELECT shape_id, ST_MakeLine(ST_SetSRID(ST_MakePoint(longitude, latitude), 4326) "
            "ORDER BY sequence) FROM {shape_point} GROUP BY shape_id HAVING count(*) >= 2"
        ).format(shape_line=name("shape_line"), shape_point=name("shape_point"))
    )


def _register(
    connection: psycopg.Connection, release: dict[str, Any], package: VerifiedPackage
) -> None:
    connection.execute(
        "INSERT INTO control.release (run_id, completed_at, gvd_year, release_json)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
        (
            package.run_id,
            release.get("completed_at"),
            release.get("gvd_year"),
            Jsonb(release),
        ),
    )
    connection.execute(
        "INSERT INTO control.package (run_id, package, feed_version, manifest_sha256,"
        " package_sha256, serving_schema_version, manifest)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_id, package) DO NOTHING",
        (
            package.run_id,
            package.package,
            package.manifest.get("feed_version", ""),
            package.manifest_sha256,
            package.package_sha256,
            package.manifest["serving_schema_version"],
            Jsonb(package.manifest),
        ),
    )
    registered = connection.execute(
        "SELECT manifest_sha256 FROM control.package WHERE run_id = %s AND package = %s",
        (package.run_id, package.package),
    ).fetchone()
    if registered is None or registered[0] != package.manifest_sha256:
        raise LoadError(
            f"{package.package}: run {package.run_id} was registered with another manifest"
        )


def _load_package(
    connection: psycopg.Connection,
    contract: Contract,
    release: dict[str, Any],
    package: VerifiedPackage,
    *,
    reload: bool,
    report: Report,
) -> LoadResult:
    started = time.monotonic()
    with connection.transaction():
        _register(connection, release, package)
        existing = connection.execute(
            "SELECT max(load_id) FROM control.load"
            " WHERE run_id = %s AND package = %s AND status = 'loaded'",
            (package.run_id, package.package),
        ).fetchone()
        existing_id = cast(int | None, existing[0] if existing else None)
        if existing_id is not None and not reload:
            report(f"{package.package}: already loaded as load {existing_id}; skipped")
            return LoadResult(package.package, existing_id, True, 0.0)
        row = connection.execute(
            "INSERT INTO control.load (run_id, package, status) VALUES (%s, %s, 'loading')"
            " RETURNING load_id",
            (package.run_id, package.package),
        ).fetchone()
        load_id = cast(int, row[0] if row else None)

    tables = static_tables(contract)
    warnings: dict[str, dict[str, int]] = {}
    try:
        with connection.transaction():
            for table in tables:
                _create_partition_table(connection, table, load_id)
            counts: dict[str, int] = {}
            for relation in contract.relations:
                relation_started = time.monotonic()
                counts[relation.name] = _copy_relation(
                    connection, relation, package.relation_paths[relation.name], load_id
                )
                report(
                    f"{package.package}: {relation.name} {counts[relation.name]:,} rows "
                    f"({time.monotonic() - relation_started:.1f} s)"
                )
            mismatched = [
                f"{name} {counts[name]} != {expected}"
                for name, expected in package.row_counts.items()
                if counts[name] != expected
            ]
            if mismatched:
                raise LoadError(f"Row counts differ from the manifest: {'; '.join(mismatched)}")
            phase = time.monotonic()
            _derive(connection, load_id)
            for table in tables:
                _build_indexes(connection, table, load_id)
            report(f"{package.package}: derived and indexed ({time.monotonic() - phase:.1f} s)")
            phase = time.monotonic()
            problems = foreign_key_violations(connection, contract, load_id)
            if problems:
                raise LoadError("Foreign keys violated:\n  " + "\n  ".join(problems))
            warnings = unknown_enum_values(connection, contract, load_id)
            report(f"{package.package}: checked ({time.monotonic() - phase:.1f} s)")
            for table in tables:
                connection.execute(
                    sql.SQL("ALTER TABLE {} ATTACH PARTITION {} FOR VALUES IN ({})").format(
                        _parent(table.name),
                        partition_name(table.name, load_id),
                        sql.Literal(load_id),
                    )
                )
            for table in tables:
                if table.derived:
                    derived = connection.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(
                            partition_name(table.name, load_id)
                        )
                    ).fetchone()
                    counts[table.name] = cast(int, derived[0] if derived else 0)
            connection.execute(
                "UPDATE control.load SET status = 'loaded', finished_at = now(),"
                " row_counts = %s, warnings = %s WHERE load_id = %s",
                (Jsonb(counts), Jsonb(warnings), load_id),
            )
    except Exception as error:
        with connection.transaction():
            connection.execute(
                "UPDATE control.load SET status = 'failed', finished_at = now(), error = %s"
                " WHERE load_id = %s",
                (str(error), load_id),
            )
        if isinstance(error, LoadError | psycopg.Error):
            raise LoadError(f"{package.package}: load {load_id} failed: {error}") from error
        raise
    seconds = time.monotonic() - started
    if warnings:
        report(f"{package.package}: unknown enumeration values: {json.dumps(warnings)}")
    report(f"{package.package}: loaded as load {load_id} ({seconds:.1f} s)")
    return LoadResult(package.package, load_id, False, seconds)


def load_release(
    connection: psycopg.Connection,
    release_dir: Path,
    contract: Contract,
    *,
    packages: Sequence[str] = PACKAGES,
    reload: bool = False,
    report: Report = print,
) -> list[LoadResult]:
    """Verify every requested package first, then load them one by one.

    Needs an autocommit connection; transactions are managed here.
    """

    release = read_release(release_dir)
    verified = [verify_package(release_dir, release, package, contract) for package in packages]
    locked = connection.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK,)).fetchone()
    if not locked or not locked[0]:
        raise LoadError("Another release load is running")
    try:
        with connection.transaction():
            connection.execute(
                "UPDATE control.load SET status = 'failed', finished_at = now(),"
                " error = 'abandoned: the loading process ended without finishing'"
                " WHERE status = 'loading'"
            )
        return [
            _load_package(connection, contract, release, package, reload=reload, report=report)
            for package in verified
        ]
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (_LOCK,))
