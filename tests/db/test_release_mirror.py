from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest

from obehy.release import activate as activation
from obehy.release.contract import Contract, load_contract
from obehy.release.load import LoadError, load_release
from tests.db.serving_fixture import Rows, czptt_rows, jdf_rows, write_release

pytestmark = pytest.mark.postgres

CONTRACT = load_contract()


def _quiet(_: str) -> None:
    pass


def _load(connection: psycopg.Connection, release_dir: Path, **options: Any) -> list[Any]:
    return load_release(connection, release_dir, CONTRACT, report=_quiet, **options)


def _scalar(connection: psycopg.Connection, query: LiteralString, *params: object) -> Any:
    row = connection.execute(query, params or None).fetchone()
    assert row is not None
    return row[0]


def _leftover_tables(connection: psycopg.Connection) -> list[str]:
    return [
        name
        for (name,) in connection.execute(
            "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'static' AND c.relkind = 'r' ORDER BY 1"
        ).fetchall()
    ]


def _activate(connection: psycopg.Connection, run_id: str) -> activation.Publication:
    return activation.activate(connection, CONTRACT, run_id)


def test_load_and_activate_exposes_both_feeds(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    results = _load(connection, write_release(tmp_path, "run-a"))
    assert [result.skipped for result in results] == [False, False]
    assert _scalar(connection, "SELECT count(*) FROM active.trip") == 0

    publication = _activate(connection, "run-a")

    assert publication.run_id == "run-a"
    trips = connection.execute("SELECT trip_id FROM active.trip ORDER BY 1").fetchall()
    assert trips == [("czptt:trip:1:1",), ("jdf:trip:1",)]
    assert (
        _scalar(
            connection,
            "SELECT name FROM active.location WHERE location_id = 'jdf:stop:1'",
        )
        == "Česká Lípa,,aut.nádr."
    )
    assert (
        _scalar(
            connection,
            "SELECT ST_AsText(geom) FROM active.location WHERE location_id = 'jdf:stop:1'",
        )
        == "POINT(14.54 50.68)"
    )
    assert _scalar(
        connection, "SELECT geom IS NULL FROM active.location WHERE location_id = 'jdf:stop:2'"
    )
    assert _scalar(connection, "SELECT ST_NPoints(geom) FROM active.shape_line") == 2
    assert _scalar(connection, "SELECT group_code FROM active.travel_restriction") == "§"
    assert (
        _scalar(
            connection,
            "SELECT public_id FROM active.source_key"
            " WHERE namespace = 'czptt:train_number' AND identifier = '6600'",
        )
        == "czptt:trip:1:1"
    )
    rows = dict(
        connection.execute(
            "SELECT package, status FROM control.load WHERE run_id = 'run-a'"
        ).fetchall()
    )
    assert rows == {"jdf": "loaded", "czptt": "loaded"}


def test_service_dates_follow_mask_and_exceptions(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    _load(connection, write_release(tmp_path, "run-a"))
    _activate(connection, "run-a")

    dates = [
        value
        for (value,) in connection.execute(
            "SELECT service_date FROM active.service_date WHERE service_id = 'jdf:service:1'"
            " ORDER BY 1"
        ).fetchall()
    ]
    # Mon 5, Tue 6, (Wed 7 removed), Thu 8, Fri 9, Sat 10 added; Sun 11 never.
    assert dates == [date(2026, 10, day) for day in (5, 6, 8, 9, 10)]
    assert _scalar(
        connection,
        "SELECT array_agg(service_date) FROM active.service_date"
        " WHERE service_id = 'czptt:service:1'",
    ) == [date(2026, 10, 7)]


def test_departures_join_across_feeds(connection: psycopg.Connection, tmp_path: Path) -> None:
    _load(connection, write_release(tmp_path, "run-a"))
    _activate(connection, "run-a")

    departures = connection.execute(
        """
        SELECT t.trip_id, c.scheduled_departure
        FROM active.location l
        JOIN active.trip_call c ON c.location_id = l.location_id
        JOIN active.trip t ON t.trip_id = c.trip_id
        JOIN active.service_date d ON d.service_id = t.service_id
        WHERE d.service_date = %s AND c.passenger_service AND c.pickup_type <> 1
          AND ST_DWithin(l.geom::geography,
                         ST_SetSRID(ST_MakePoint(14.54, 50.68), 4326)::geography, 1000)
        ORDER BY c.scheduled_departure
        """,
        (date(2026, 10, 7),),
    ).fetchall()
    # Wednesday 7th: the JDF service is removed, the train runs.
    assert departures == [("czptt:trip:1:1", 6 * 3600 + 360)]


def test_rollback_restores_the_previous_release(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    second = jdf_rows()
    second["route"][0]["short_name"] = "2"
    _load(connection, write_release(tmp_path, "run-a"))
    _load(connection, write_release(tmp_path, "run-b", jdf=second))
    _activate(connection, "run-a")
    _activate(connection, "run-b")
    assert _scalar(connection, "SELECT short_name FROM active.route WHERE mode = 'bus'") == "2"

    publication = activation.rollback(connection, CONTRACT)

    assert publication.run_id == "run-a"
    assert _scalar(connection, "SELECT short_name FROM active.route WHERE mode = 'bus'") == "1"
    with pytest.raises(activation.ActivationError, match="no predecessor"):
        activation.rollback(connection, CONTRACT)
    actions = connection.execute(
        "SELECT run_id, action FROM control.publication_history ORDER BY seq"
    ).fetchall()
    assert actions == [("run-a", "activate"), ("run-b", "activate"), ("run-a", "rollback")]


def test_activate_if_newer_skips_older_releases_and_respects_a_rollback(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    for run_id in ("20261008T000000Z-a", "20261009T000000Z-b", "20261007T000000Z-c"):
        _load(connection, write_release(tmp_path, run_id))

    first = activation.activate_if_newer(connection, CONTRACT, "20261008T000000Z-a")
    assert first is not None and first.run_id == "20261008T000000Z-a"
    assert activation.activate_if_newer(connection, CONTRACT, "20261008T000000Z-a") is None
    assert activation.activate_if_newer(connection, CONTRACT, "20261007T000000Z-c") is None
    assert activation.activate_if_newer(connection, CONTRACT, "20261009T000000Z-b") is not None
    activation.rollback(connection, CONTRACT)

    assert activation.activate_if_newer(connection, CONTRACT, "20261009T000000Z-b") is None
    assert _scalar(connection, "SELECT run_id FROM control.publication") == "20261008T000000Z-a"


def test_activate_requires_both_packages(connection: psycopg.Connection, tmp_path: Path) -> None:
    _load(connection, write_release(tmp_path, "run-a"), packages=("jdf",))
    with pytest.raises(activation.ActivationError, match="no loaded czptt"):
        _activate(connection, "run-a")


Mutation = Callable[[Rows], object]
BROKEN_PACKAGES: list[tuple[Mutation, str]] = [
    (lambda rows: rows["trip_call"][1].update(location_id="jdf:stop:404"), "Foreign keys"),
    (lambda rows: rows["agency"].append(dict(rows["agency"][0])), "unique index"),
    (lambda rows: rows["trip"][0].update(route_id="jdf:route:404"), r"trip\(route_id\)"),
]


@pytest.mark.parametrize(("mutate", "message"), BROKEN_PACKAGES)
def test_failed_load_leaves_nothing_attached(
    connection: psycopg.Connection,
    tmp_path: Path,
    mutate: Mutation,
    message: str,
) -> None:
    rows = jdf_rows()
    mutate(rows)
    with pytest.raises(LoadError, match=message):
        _load(connection, write_release(tmp_path, "run-bad", jdf=rows), packages=("jdf",))

    assert _leftover_tables(connection) == []
    assert _scalar(connection, "SELECT count(*) FROM static.trip") == 0
    status, error = connection.execute(
        "SELECT status, error FROM control.load WHERE run_id = 'run-bad'"
    ).fetchone() or (None, None)
    assert status == "failed"
    assert error


def test_row_count_mismatch_fails(connection: psycopg.Connection, tmp_path: Path) -> None:
    def patch(manifest: dict[str, Any]) -> None:
        for relation in manifest["relations"]:
            if relation["name"] == "trip":
                relation["row_count"] = 2

    with pytest.raises(LoadError, match="Row counts differ"):
        _load(connection, write_release(tmp_path, "run-bad", manifest_patch=patch))
    assert _leftover_tables(connection) == []


def test_tampered_file_is_rejected_before_loading(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    release_dir = write_release(tmp_path, "run-a")
    path = release_dir / "czptt" / "serving" / "trip.parquet"
    data = bytearray(path.read_bytes())
    data[-10] ^= 0xFF
    path.write_bytes(bytes(data))

    with pytest.raises(LoadError, match="does not match its manifest hash"):
        _load(connection, release_dir)
    assert _scalar(connection, "SELECT count(*) FROM control.load") == 0


def test_reload_is_skipped_unless_forced(connection: psycopg.Connection, tmp_path: Path) -> None:
    release_dir = write_release(tmp_path, "run-a")
    first = _load(connection, release_dir)
    again = _load(connection, release_dir)
    forced = _load(connection, release_dir, reload=True)

    assert [result.skipped for result in again] == [True, True]
    assert [result.load_id for result in again] == [result.load_id for result in first]
    assert all(
        new.load_id is not None and old.load_id is not None and new.load_id > old.load_id
        for new, old in zip(forced, first, strict=True)
    )


def test_unknown_enum_value_is_a_warning(connection: psycopg.Connection, tmp_path: Path) -> None:
    rows = czptt_rows()
    rows["route"][0]["mode"] = "hovercraft"
    _load(connection, write_release(tmp_path, "run-a", czptt=rows), packages=("czptt",))

    warnings = _scalar(connection, "SELECT warnings FROM control.load WHERE package = 'czptt'")
    assert warnings == {"route.mode": {"hovercraft": 1}}


def test_prune_keeps_active_and_predecessors(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    contract: Contract = CONTRACT
    for run_id in ("run-a", "run-b", "run-c", "run-d"):
        _load(connection, write_release(tmp_path, run_id))
        _activate(connection, run_id)
    _load(connection, write_release(tmp_path, "run-e"))  # staged, not active yet

    dropped = activation.prune(connection, contract, keep=2)

    statuses = dict(
        connection.execute(
            "SELECT run_id, array_agg(DISTINCT status) FROM control.load GROUP BY run_id"
        ).fetchall()
    )
    assert statuses == {
        "run-a": ["dropped"],
        "run-b": ["loaded"],
        "run-c": ["loaded"],
        "run-d": ["loaded"],
        "run-e": ["loaded"],
    }
    assert len(dropped) == 2
    assert not [name for name in _leftover_tables(connection) if name.endswith(f"_l{dropped[0]}")]
    assert _scalar(connection, "SELECT count(*) FROM control.release") == 5
    # The remaining views still work after dropping partitions.
    assert _scalar(connection, "SELECT count(*) FROM active.trip") == 2
