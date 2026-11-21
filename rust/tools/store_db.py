"""Test databases for obehy-rt's database tests (`obehy-rt/tests/store_db.rs`, `worker_db.rs`):
what only the Python side does (migrations, release loading), and a dump to compare databases.

    uv run python rust/tools/store_db.py create [--name NAME]     # prints the database name
    uv run python rust/tools/store_db.py drop NAME
    uv run python rust/tools/store_db.py dump NAME OUT
    uv run python rust/tools/store_db.py broker-fixture NAME
    uv run python rust/tools/store_db.py release NAME [NAME ...] --plan PLAN

`create` makes a database from the test database URL (`OBEHY_TEST_DATABASE_URL` or
`[database] test_url`), migrated as `tests/db/conftest.py` migrates its template, and prints only
its name: the URL never leaves this process. `dump` writes every `rt.*` and `history.*` table,
row for row in a total order, as canonical JSON lines, so two databases compare as text.
`broker-fixture` loads `tests/db/test_api.py`'s small release with a rail run's live state and
the lookup requests the API would record. `release` loads a bounded subset of a real release
into one or more databases under one run ID and activates it (printed last): the rows the keys
of the plan (`{"feed", "release", "refs": [[namespace, key]], "lines": [[namespace, line]]}`,
which `worker_db.rs` collects from an archive window) reach. Never the whole feed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psycopg
from psycopg import conninfo, sql

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from obehy.release import activate as activation  # noqa: E402
from obehy.release.contract import load_contract  # noqa: E402
from obehy.release.load import load_release  # noqa: E402
from obehy.release.migrate import migrate  # noqa: E402
from obehy.runtime_config import load_test_database_url  # noqa: E402
from tests.db.worker_rows import write_physical_policy  # noqa: E402


def admin_url() -> str:
    url = load_test_database_url()
    if not url:
        sys.exit("no test database: set OBEHY_TEST_DATABASE_URL or [database] test_url")
    return url


def url_of(name: str) -> str:
    return conninfo.make_conninfo(admin_url(), dbname=name)


def create(name: str | None) -> str:
    name = name or f"obehy_rs_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(url_of(name), autocommit=True) as connection:
        migrate(connection)
        write_physical_policy(connection)
    return name


def drop(name: str) -> None:
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def dump(name: str, out: Path) -> int:
    count = 0
    with (
        psycopg.connect(url_of(name), autocommit=True) as connection,
        out.open("w", encoding="utf-8", newline="\n") as stream,
    ):
        connection.execute("SET TIME ZONE 'UTC'")
        tables = connection.execute(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n"
            " ON n.oid = c.relnamespace WHERE n.nspname IN ('rt', 'history')"
            " AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY 1, 2"
        ).fetchall()
        for schema, table in tables:
            columns = [
                r[0]
                for r in connection.execute(
                    "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass"
                    " AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                    (f"{schema}.{table}",),
                ).fetchall()
            ]
            query = sql.SQL("SELECT to_jsonb(t)::text FROM {}.{} t ORDER BY {}").format(
                sql.Identifier(schema),
                sql.Identifier(table),
                sql.SQL(", ").join(
                    sql.SQL('t.{}::text COLLATE "C"').format(sql.Identifier(c)) for c in columns
                ),
            )
            for (text,) in connection.execute(query).fetchall():
                stream.write(f"{schema}.{table}\t{text}\n")
                count += 1
    return count


def broker_fixture(name: str) -> None:
    """`tests/db/test_api.py`'s small release (two jdf trips, one rail run of two parts) loaded
    and active, the rail run's live state (left its first two calls), and the lookup requests
    the API would record (`api/sql/request_lookup.sql`) for the run's parts and a bus."""

    from tests.db.serving_fixture import write_release
    from tests.db.test_api import CONTRACT, RUN, _call, _czptt, _jdf, _state

    with (
        psycopg.connect(url_of(name), autocommit=True) as connection,
        tempfile.TemporaryDirectory() as tmp,
    ):
        release = write_release(Path(tmp), "run-a", jdf=_jdf(), czptt=_czptt())
        load_release(connection, release, CONTRACT, report=lambda _: None)
        activation.activate(connection, CONTRACT, "run-a")
        _state(
            connection,
            ("czptt", "czptt:pa", RUN, "2026-10-07"),
            f"run:{RUN}:1,2",
            [
                _call(1, "czptt:location:CZ54000", "actual", "2026-10-07T06:07:00"),
                _call(2, "czptt:location:CZ54001", "actual", "2026-10-07T06:09:00"),
                _call(3, "czptt:location:CZ54002", "predicted", "2026-10-07T06:19:00"),
                _call(4, "czptt:location:CZ54003", "predicted", "2026-10-07T06:33:00"),
            ],
            [("czptt:trip:1:1", 0, 2), ("czptt:trip:1:2", 2, 3)],
        )
        request = (REPO / "src/obehy/api/sql/request_lookup.sql").read_text(encoding="utf-8")
        for feed, trip, day, start in (
            ("czptt", "czptt:trip:1:2", "2026-10-07", ""),
            ("jdf", "jdf:trip:1", "2026-10-06", ""),
            ("czptt", "czptt:trip:1:1", "2026-10-07", ""),
        ):
            connection.execute(
                request,  # type: ignore[arg-type]
                {
                    "kind": "composition",
                    "feed": feed,
                    "trip_id": trip,
                    "service_date": day,
                    "from_location": start,
                },
            )


def _subset_release(plan: dict[str, Any], out: Path) -> Path:
    """The plan's release cut to the rows its keys can reach (bounded, never the whole feed):
    the trips the keys name with every part of their runs, their calls, routes, shapes,
    calendars and locations; all location keys and locations (one per station); the other
    feed is the fixture's."""

    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from tests.db.serving_fixture import czptt_rows, jdf_rows, write_release

    feed = plan["feed"]
    serving = Path(plan["release"]) / feed / "serving"

    def read(table: str) -> Any:
        return pq.read_table(serving / f"{table}.parquet")  # pyright: ignore

    def isin(table: Any, column: str, values: Iterable[Any]) -> Any:
        return table.filter(pc.is_in(table.column(column), value_set=pa.array(sorted(values))))  # pyright: ignore

    wanted: set[tuple[str, str]] = {(r[0], r[1]) for r in plan["refs"]}
    wanted |= {("cis:line", r[1]) for r in plan["lines"]}
    keys = read("source_key")
    lines = {i for ns, i in wanted if ns == "cis:line"}
    pairs = list(
        zip(
            keys.column("namespace").to_pylist(), keys.column("identifier").to_pylist(), strict=True
        )
    )
    kinds = keys.column("entity_kind").to_pylist()
    publics = keys.column("public_id").to_pylist()
    named = {
        p
        for (ns, i), p in zip(pairs, publics, strict=True)
        if (ns, i) in wanted or (ns == "cis:line_trip" and i.split(":", 1)[0] in lines)
    }
    trips = read("trip")
    chosen = isin(trips, "trip_id", named & set(trips.column("trip_id").to_pylist()))
    runs = {r for r in chosen.column("run_key").to_pylist() if r is not None}
    if runs:
        chosen = trips.filter(
            pc.or_(  # pyright: ignore
                pc.is_in(trips.column("trip_id"), value_set=chosen.column("trip_id")),  # pyright: ignore
                pc.is_in(trips.column("run_key"), value_set=pa.array(sorted(runs))),  # pyright: ignore
            )
        )
    trip_ids = set(chosen.column("trip_id").to_pylist())
    keep_key = [
        (k == "location")
        or (p in trip_ids)
        or ((ns, i) in wanted)
        or (ns == "cis:line_trip" and i.split(":", 1)[0] in lines)
        for (ns, i), k, p in zip(pairs, kinds, publics, strict=True)
    ]
    rows: dict[str, list[dict[str, Any]]] = {
        "agency": read("agency").to_pylist(),
        "location": read("location").to_pylist(),
        "source_key": keys.filter(pa.array(keep_key)).to_pylist(),
        "trip": chosen.to_pylist(),
    }
    routes = set(chosen.column("route_id").to_pylist())
    route_keys = {
        p
        for (ns, i), k, p in zip(pairs, kinds, publics, strict=True)
        if k == "route" and (ns, i) in wanted
    }
    rows["route"] = isin(read("route"), "route_id", routes | route_keys).to_pylist()
    calls = isin(read("trip_call"), "trip_id", trip_ids).to_pylist()
    for call in calls:
        call["route_stop_id"] = None
    rows["trip_call"] = calls
    shapes = {s for s in chosen.column("shape_id").to_pylist() if s is not None}
    rows["shape"] = isin(read("shape"), "shape_id", shapes).to_pylist() if shapes else []
    rows["shape_point"] = (
        isin(read("shape_point"), "shape_id", shapes).to_pylist() if shapes else []
    )
    services = set(chosen.column("service_id").to_pylist())
    rows["service_calendar"] = isin(read("service_calendar"), "service_id", services).to_pylist()
    rows["service_exception"] = isin(read("service_exception"), "service_id", services).to_pylist()
    for trip in rows["trip"]:
        trip["block_key"] = None
    other = "jdf" if feed == "czptt" else "czptt"
    fixture = {"jdf": jdf_rows, "czptt": czptt_rows}[other]()
    run_id = f"subset-{uuid.uuid4().hex[:8]}"
    release = write_release(out, run_id, **{feed: rows, other: fixture})
    print(
        f"subset release: {len(rows['trip'])} trips, {len(rows['trip_call'])} calls,"
        f" {len(rows['shape_point'])} shape points"
    )
    return release


def subset_release(names: list[str], plan_file: Path) -> str:
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        subset = _subset_release(plan, Path(tmp) / "subset")
        run_id = json.loads((subset / "release.json").read_text(encoding="utf-8"))["run_id"]
        contract = load_contract()
        for name in names:
            with psycopg.connect(url_of(name), autocommit=True) as connection:
                load_release(connection, subset, contract, report=lambda _: None)
                activation.activate(connection, contract, run_id)
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("create")
    p.add_argument("--name")
    p = commands.add_parser("drop")
    p.add_argument("name")
    p = commands.add_parser("dump")
    p.add_argument("name")
    p.add_argument("out", type=Path)
    p = commands.add_parser("broker-fixture")
    p.add_argument("name")
    p = commands.add_parser("release")
    p.add_argument("names", nargs="+")
    p.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "create":
        print(create(args.name))
    elif args.command == "drop":
        drop(args.name)
    elif args.command == "broker-fixture":
        broker_fixture(args.name)
    elif args.command == "release":
        print(subset_release(args.names, args.plan))
    else:
        print(dump(args.name, args.out))


if __name__ == "__main__":
    main()
