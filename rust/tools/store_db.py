"""Test databases for the Rust store (docs/RUST_PORT.md, R7), and the Python side of its parity.

    uv run python rust/tools/store_db.py create [--name NAME]     # prints the database name
    uv run python rust/tools/store_db.py drop NAME
    uv run python rust/tools/store_db.py apply NAME STREAM [--limit N]
    uv run python rust/tools/store_db.py dump NAME OUT
    uv run python rust/tools/store_db.py parity STREAM [--limit N] [--keep]
    uv run python rust/tools/store_db.py broker-fixture NAME
    uv run python rust/tools/store_db.py release NAME [NAME ...] --release DIR --archive ROOT
        --source duk --channel vehicles --since ISO --until ISO [--feed jdf]

`create` makes a database from the test database URL (`OBEHY_TEST_DATABASE_URL` or
`[database] test_url`), migrated as `tests/db/conftest.py` migrates its template, and prints only
its name: the URL never leaves this process. `apply` sends the row records of an output stream
(`obehy rt compare --level output`, `emit/recording.py`'s kinds) through Python's `SqlStore`,
in order, each step's or tick's rows in one transaction, as the writer would have sent them;
`obehy-rt`'s `store_parity` example sends the same stream through the Rust store. `dump` writes
every `rt.*` and `history.*` table, row for row in a total order, as canonical JSON lines, so two
databases compare as text (ids that sequences hand out are compared as they are: both sides
send the same rows in the same order). `parity` does it all: two fresh databases, the stream
through Python's store into one and through `obehy-rt`'s (`examples/store_parity.rs`, the URL
passed in the environment) into the other, both dumped and compared, the databases dropped.
`release` loads a bounded subset of a real release (the rows the keys of an archive window
reach) into one or more databases under one run ID and activates it (`tests/worker_db.rs`).
"""

from __future__ import annotations

import argparse
import difflib
import gzip
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import conninfo, sql

from obehy.realtime.emit.pairing import Evidence
from obehy.realtime.emit.rows import (
    ASSIGNMENT_COLUMNS,
    CANCELLATION_COLUMNS,
    EVENT_COLUMNS,
    LINK_COLUMNS,
    OBSERVATION_COLUMNS,
    PARTS,
    PLATFORM_COLUMNS,
    TRIPS,
    VEHICLES,
    StateTable,
)
from obehy.realtime.emit.sql_store import SqlStore
from obehy.realtime.emit.storage import hour_of
from obehy.realtime.emit.vehicles import VehicleRows, write_physical_policy
from obehy.realtime.model import Derivation, JourneyKey, ScheduledCall, SnapshotJourney
from obehy.reference.precedence import Profile
from obehy.release.migrate import migrate
from obehy.runtime_config import load_test_database_url

TABLES = {t.name_text: t for t in (VEHICLES, TRIPS, PARTS)}
HISTORY_DAY_KINDS = ("platform_evidence", "journey_cancellation")


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


# --- apply: a stream's rows through SqlStore ----------------------------------------------------


def _time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text.replace("Z", "+00:00"))


def _date(text: str | None) -> date | None:
    return None if text is None else date.fromisoformat(text)


TIME_COLUMNS = frozenset(
    {
        "received_at",
        "observed_at",
        "state_since",
        "event_time",
        "interval_lo",
        "interval_hi",
        "first_seen",
        "last_seen",
        "at",
        "updated_at",
        "position_at",
    }
)


def _cell(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in TIME_COLUMNS:
        return _time(value)
    if column == "service_date":
        return _date(value)
    if column in ("calls", "sources"):
        return json.dumps(value)
    return value


def _row(columns: tuple[str, ...], row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(_cell(c, row[c]) for c in columns)


def _journey(value: dict[str, Any]) -> JourneyKey:
    return JourneyKey(value["feed"], value["namespace"], value["key"], _date(value["service_date"]))  # type: ignore[arg-type]


def _snapshot(value: dict[str, Any]) -> SnapshotJourney:
    return SnapshotJourney(
        journey=_journey(value["journey"]),
        at=_time(value["at"]),  # type: ignore[arg-type]
        release_id=value["release_id"],
        trip_id=value["trip_id"],
        route_name=value["route_name"],
        headsign=value["headsign"],
        calls=tuple(
            ScheduledCall(
                ordinal=c["ordinal"],
                location_id=c["location_id"],
                visit_n=c["visit_n"],
                passenger_service=c["passenger_service"],
                scheduled_arrival=c["scheduled_arrival"],
                scheduled_departure=c["scheduled_departure"],
                name=c["name"],
            )
            for c in value["calls"]
        ),
        run_key=value["run_key"],
        route_id=value["route_id"],
        mode=value["mode"],
        agency_id=value["agency_id"],
    )


def _profile(value: dict[str, Any]) -> Profile:
    return Profile(
        vehicle_id=value["vehicle_id"],
        registered=value["registered"],
        public_label=value["public_label"],
        flags=value["flags"],
        texts=value["texts"],
        datasets=tuple(value["datasets"]),
        sources=tuple(value["sources"]),
        attributes=tuple(value["attributes"]),
        last_stated=_time(value["last_stated"]),
    )


def _evidence(value: dict[str, Any]) -> Evidence:
    return Evidence(
        _time(value["first_seen"]),  # type: ignore[arg-type]
        _time(value["last_seen"]),  # type: ignore[arg-type]
        value["fixes"],
        _journey(value["journey"]),
        value["confirmed"],
    )


ROW_COLUMNS = {
    "observation": OBSERVATION_COLUMNS,
    "stop_event": EVENT_COLUMNS,
    "vehicle_assignment": ASSIGNMENT_COLUMNS,
    "journey_link": LINK_COLUMNS,
    "platform_evidence": PLATFORM_COLUMNS,
    "journey_cancellation": CANCELLATION_COLUMNS,
}


def groups(path: Path, limit: int | None) -> Iterator[list[dict[str, Any]]]:
    """The stream's row records, one group per write: the rows after an effect, header or
    GTFS-RT record, split again where state rows start (a snapshot's `write_state` follows a
    tick's `write` with nothing between them)."""

    opener = gzip.open if path.suffix == ".gz" else open
    group: list[dict[str, Any]] = []
    count = 0
    with opener(path, "rt", encoding="utf-8") as stream:  # type: ignore[operator]
        for line in stream:
            record = json.loads(line)
            kind = record["kind"]
            if kind in ("header", "effect", "gtfs_rt", "poll", "predictor_sample"):
                if group:
                    yield group
                    group = []
                continue
            if (
                kind == "state"
                and group
                and group[-1]["kind"] != "state"
                and not any(r["kind"] == "state" for r in group)
            ):
                yield group
                group = []
            group.append(record)
            count += 1
            if limit is not None and count >= limit:
                break
    if group:
        yield group


def apply(name: str, stream: Path, limit: int | None, release: str) -> int:
    count = 0
    with psycopg.connect(url_of(name), autocommit=True) as connection:
        connection.execute("SET TIME ZONE 'UTC'")
        store = SqlStore(connection)
        derivation = Derivation("r1.0", "18", release)
        store.open(derivation)
        for group in groups(stream, limit):
            count += len(group)
            if group[0]["kind"] == "physical_vehicles":
                continue  # written by open
            if group[0]["kind"] == "state":
                with store.transaction():
                    _states(store, [r for r in group if r["kind"] == "state"])
                    _vehicles(store, [r for r in group if r["kind"] != "state"])
                continue
            hours = {
                hour_of(_time(r["row"]["received_at"]))  # type: ignore[arg-type]
                for r in group
                if r["kind"] == "observation"
            }
            days: set[date] = set()
            terms: list[str] = []
            for r in group:
                if r["kind"] == "journey_schedule":
                    days.add(_date(r["snapshot"]["journey"]["service_date"]))  # type: ignore[arg-type]
                    for c in r["snapshot"]["calls"]:
                        terms += [c["location_id"], c["name"]]
                elif r["kind"] in HISTORY_DAY_KINDS:
                    days.add(_date(r["row"]["service_date"]))  # type: ignore[arg-type]
                elif r["kind"] == "stop_event":
                    terms += [r["row"]["location"], r["row"]["method"], r["row"]["source"]]
            store.prepare(hours, days, terms)
            with store.transaction():
                _effect_rows(store, group)
    return count


def _of(group: list[dict[str, Any]], kind: str) -> list[tuple[Any, ...]]:
    columns = tuple(ROW_COLUMNS[kind])
    rows = []
    for r in group:
        if r["kind"] == kind:
            row = r["row"]
            rows.append(_row(columns, row))
    return rows


def _effect_rows(store: SqlStore, group: list[dict[str, Any]]) -> None:
    observations = _of(group, "observation")
    # SqlStore sends `facts` as JSON of the dict.
    store.observations(observations)
    for r in group:
        if r["kind"] == "journey_schedule":
            store.snapshot(_snapshot(r["snapshot"]))
    store.events(_of(group, "stop_event"))
    store.assignments(_of(group, "vehicle_assignment"))
    store.links(_of(group, "journey_link"))
    store.platforms(_of(group, "platform_evidence"))
    store.cancellations(_of(group, "journey_cancellation"))
    _vehicles(store, group)


def _vehicles(store: SqlStore, group: list[dict[str, Any]]) -> None:
    attributes = [
        (
            r["row"]["source"],
            r["row"]["source_vehicle_id"],
            r["row"]["attribute"],
            r["row"]["value"],
            _time(r["row"]["observed_at"]),
        )
        for r in group
        if r["kind"] == "vehicle_attribute"
    ]
    gone = [
        (tuple(r["a"]), tuple(r["b"]))
        for r in group
        if r["kind"] == "vehicle_pairing" and r["op"] == "delete"
    ]
    kept = [
        ((tuple(r["a"]), tuple(r["b"])), _evidence(r["evidence"]))
        for r in group
        if r["kind"] == "vehicle_pairing" and r["op"] == "upsert"
    ]
    rewrite = any(r["kind"] == "vehicle_profile_reset" for r in group)
    profiles = [_profile(r["profile"]) for r in group if r["kind"] == "vehicle_profile"]
    store.vehicles(VehicleRows(attributes, gone, kept, rewrite, profiles))  # type: ignore[arg-type]


def _states(store: SqlStore, records: list[dict[str, Any]]) -> None:
    """A `write_state`'s calls again: `replace` with its inserts, runs of deletes and of
    upserts of one table."""

    i = 0
    while i < len(records):
        record = records[i]
        table: StateTable = TABLES[record["table"]]
        op = record["op"]
        j = i + 1
        if op == "replace":
            while j < len(records) and records[j]["op"] == "insert":
                j += 1
            rows = [_row(table.columns, r["row"]) for r in records[i + 1 : j]]
            store.replace_state(table, record["feed"], rows)
        else:
            same = record["table"]
            while j < len(records) and records[j]["op"] == op and records[j]["table"] == same:
                j += 1
            if op == "delete":
                store.delete_state(table, [_row(table.key, r["key"]) for r in records[i:j]])
            else:
                store.upsert_state(table, [_row(table.columns, r["row"]) for r in records[i:j]])
        i = j


# --- dump -------------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


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


RUST = Path(__file__).resolve().parent.parent


def broker_fixture(name: str) -> None:
    """`tests/db/test_api.py`'s small release (two jdf trips, one rail run of two parts) loaded
    and active, the rail run's live state (left its first two calls), and the lookup requests
    the API would record (`api/sql/request_lookup.sql`) for the run's parts and a bus."""

    import tempfile

    sys.path.insert(0, str(RUST.parent))
    from obehy.release import activate as activation
    from obehy.release.load import load_release
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
        request = (RUST.parent / "src/obehy/api/sql/request_lookup.sql").read_text(encoding="utf-8")
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


def subset_release(
    names: list[str],
    release: Path,
    archive: Path,
    source: str,
    channel: str,
    since: str,
    until: str,
    feed: str,
) -> str:
    """A bounded subset of `release` loaded into each database and activated: the rows the keys
    of the window's polls reach in `feed` (`index_crosscheck._subset_release`; the other feed is
    the fixture's), under one fresh run ID, which it prints last. Never the whole feed."""

    import tempfile

    sys.path.insert(0, str(RUST.parent))
    sys.path.insert(0, str(RUST / "tools"))
    from index_crosscheck import _subset_release  # pyright: ignore

    from obehy.realtime.policy import load_policy
    from obehy.realtime.replay import ReplayOptions, decoded_polls, named_channels
    from obehy.realtime.runner import _lines, _refs  # pyright: ignore
    from obehy.realtime.times import instant
    from obehy.release.activate import activate
    from obehy.release.contract import load_contract
    from obehy.release.load import load_release

    start, end = instant(_time(since)), instant(_time(until))  # type: ignore[arg-type]
    refs: set[tuple[str, str]] = set()
    lines: set[tuple[str, str]] = set()
    with tempfile.TemporaryDirectory() as tmp:
        options = ReplayOptions(
            release=release,
            archive=archive,
            start=start.date(),
            end=end.date(),
            channels=named_channels([(source, channel)]),
            out=Path(tmp),
            since=start,
            until=end,
            all_feeds=True,
        )
        for poll in decoded_polls(options, load_policy()):
            for observation in poll.observations or []:
                if observation.feed == feed:
                    refs |= _refs(observation)
                    lines |= _lines(observation)
        plan = {
            "feed": feed,
            "release": str(release),
            "steps": [
                {
                    "op": "ensure",
                    "refs": [list(r) for r in sorted(refs)],
                    "lines": [list(r) for r in sorted(lines)],
                }
            ],
        }
        subset = _subset_release(plan, Path(tmp) / "subset")
        run_id = json.loads((subset / "release.json").read_text(encoding="utf-8"))["run_id"]
        contract = load_contract()
        for name in names:
            with psycopg.connect(url_of(name), autocommit=True) as connection:
                load_release(connection, subset, contract, report=lambda _: None)
                activate(connection, contract, run_id)
    return run_id


def _header(stream: Path) -> dict[str, Any]:
    opener = gzip.open if stream.suffix == ".gz" else open
    with opener(stream, "rt", encoding="utf-8") as lines:  # type: ignore[operator]
        return json.loads(next(iter(lines)))


def parity(stream: Path, limit: int | None, keep: bool, work: Path) -> int:
    release = _header(stream)["release"]
    created: list[str] = []
    try:
        created.append(create(None))
        created.append(create(None))
        python_db, rust_db = created
        started = time.monotonic()
        count = apply(python_db, stream, limit, release)
        print(f"python: {count} records in {time.monotonic() - started:.1f} s")
        built = RUST / "target" / "release" / "examples" / "store_parity.exe"
        if not built.exists():
            built = built.with_suffix("")
        command = (
            [str(built)]
            if built.exists() and os.environ.get("STORE_PARITY_BUILT")
            else [
                "cargo",
                "run",
                "--release",
                "-q",
                "-p",
                "obehy-rt",
                "--example",
                "store_parity",
                "--",
            ]
        )
        command += ["--stream", str(stream.resolve()), "--url-env", "OBEHY_STORE_PARITY_URL"]
        if limit is not None:
            command += ["--limit", str(limit)]
        if os.environ.get("STORE_PARITY_COPY_FROM"):
            command += ["--copy-from", os.environ["STORE_PARITY_COPY_FROM"]]
        environment = {**os.environ, "OBEHY_STORE_PARITY_URL": url_of(rust_db)}
        subprocess.run(command, cwd=RUST, env=environment, check=True)
        work.mkdir(parents=True, exist_ok=True)
        left, right = work / "python.tsv", work / "rust.tsv"
        rows = dump(python_db, left), dump(rust_db, right)
        print(f"dumped {rows[0]} and {rows[1]} rows")
        a = left.read_text(encoding="utf-8").splitlines()
        b = right.read_text(encoding="utf-8").splitlines()
        if a == b:
            tables: dict[str, int] = {}
            for line in a:
                name = line.split("	", 1)[0]
                tables[name] = tables.get(name, 0) + 1
            print("identical: " + ", ".join(f"{k} {v}" for k, v in sorted(tables.items())))
            return 0
        diff = list(difflib.unified_diff(a, b, "python", "rust", n=0, lineterm=""))
        print(f"{len(diff)} diff lines; first ones:")
        for line in diff[:40]:
            print(line[:400])
        return 1
    finally:
        if not keep:
            for name in created:
                drop(name)
        else:
            print(f"kept {created} (python, rust)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("create")
    p.add_argument("--name")
    p = commands.add_parser("drop")
    p.add_argument("name")
    p = commands.add_parser("apply")
    p.add_argument("name")
    p.add_argument("stream", type=Path)
    p.add_argument("--limit", type=int)
    p.add_argument("--release", default="")
    p = commands.add_parser("dump")
    p.add_argument("name")
    p.add_argument("out", type=Path)
    p = commands.add_parser("broker-fixture")
    p.add_argument("name")
    p = commands.add_parser("release")
    p.add_argument("names", nargs="+")
    p.add_argument("--release", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--since", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--feed", default="jdf")
    p = commands.add_parser("parity")
    p.add_argument("stream", type=Path)
    p.add_argument("--limit", type=int)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--work", type=Path, default=Path("data/store-parity"))
    args = parser.parse_args()
    if args.command == "create":
        print(create(args.name))
    elif args.command == "drop":
        drop(args.name)
    elif args.command == "apply":
        print(apply(args.name, args.stream, args.limit, args.release))
    elif args.command == "broker-fixture":
        broker_fixture(args.name)
    elif args.command == "release":
        print(
            subset_release(
                args.names,
                args.release,
                args.archive,
                args.source,
                args.channel,
                args.since,
                args.until,
                args.feed,
            )
        )
    elif args.command == "parity":
        sys.exit(parity(args.stream, args.limit, args.keep, args.work))
    else:
        print(dump(args.name, args.out))


if __name__ == "__main__":
    main()
