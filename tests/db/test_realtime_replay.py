"""Replay, warm restart and release switch end to end (docs/R1_SLICE.md section 6)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.index_sql import ensure_release, run_loads
from obehy.realtime.policy import load_policy
from obehy.realtime.replay import ReplayOptions, replay
from obehy.realtime.runner import Runner
from obehy.realtime.times import Instant, instant
from obehy.realtime.worker import warm_start
from tests.db.serving_fixture import write_release
from tests.realtime.builder import ORIGIN, STEP_LON, Timetable, serving_rows, timetable

pytestmark = pytest.mark.postgres

POLICY = load_policy()
DAY = date(2026, 10, 8)
START = datetime(2026, 10, 8, 5, 50, tzinfo=UTC)  # 07:50 CEST


def _quiet(_: str) -> None:
    pass


def _timetable(*, drop_b: bool = False) -> Timetable:
    calls = [("A", "08:00"), ("B", "08:10"), ("C", "08:20")]
    if drop_b:
        calls = [("A", "08:00"), ("C", "08:20")]
    tt = timetable().stop("A", ORIGIN[0], ORIGIN[1])
    tt.stop("B", ORIGIN[0] + STEP_LON, ORIGIN[1]).stop("C", ORIGIN[0] + 2 * STEP_LON, ORIGIN[1])
    return tt.trip("582492:143", days=[DAY], calls=calls)


def _archive(root: Path) -> Path:
    """Two buses polled every 30 s for 40 minutes: one runs 582492:143, one an unknown key."""

    writer = ArchiveWriter(root)
    for n in range(80):
        at = START + timedelta(seconds=30 * n)
        minutes = (at - START).total_seconds() / 60 - 10  # minutes after 08:00 local
        metres = max(0.0, min(2000.0, minutes * 100))
        vehicles: list[dict[str, Any]] = [
            {
                "ID": 810,
                "Delay": 1,
                "RouteID": 143,
                "CISLineID": 582492,
                "Longitude": ORIGIN[0] + STEP_LON * metres / 1000,
                "Latitude": ORIGIN[1],
                "GPSPositionDT": (at - timedelta(seconds=3)).isoformat(),
                "Azimut": 90.0,
                "State": 2 if minutes < 0 else 0,
            },
            {
                "ID": 811,
                "Delay": 0,
                "RouteID": 7,
                "CISLineID": 999999,
                "Longitude": 14.5,
                "Latitude": 50.5,
                "GPSPositionDT": at.isoformat(),
                "State": 0,
            },
        ]
        body = json.dumps({"VehicleList": vehicles}).encode()
        poll = Poll(at - timedelta(milliseconds=300), at, 200, body, "application/json")
        writer.append("duk", "vehicles", poll, body)
    return root


def _options(tmp_path: Path, out: str, release: Path, **extra: Any) -> ReplayOptions:
    return ReplayOptions(
        release=release,
        archive=tmp_path / "archive",
        start=DAY,
        end=DAY,
        channels=[("duk", "vehicles")],
        out=tmp_path / out,
        gtfs_rt_every_s=60,
        **extra,
    )


def _snapshots(out: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((out / "gtfs-rt" / "jdf").glob("*.pb"))}


def _history(connection: psycopg.Connection) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT location_id, visit_n, event_type, revision, interval_lo, interval_hi"
        " FROM history.actual_stop_event ORDER BY 1, 2, 3"
    ).fetchall()


@pytest.fixture
def release(tmp_path: Path) -> Path:
    _archive(tmp_path / "archive")
    return write_release(tmp_path, "run-a", jdf=serving_rows(_timetable()))


def test_replay_is_deterministic_and_rebuilds_history(
    connection: psycopg.Connection, tmp_path: Path, release: Path
) -> None:
    first = replay(
        connection, _options(tmp_path, "one", release, write_history=True), POLICY, report=_quiet
    )
    events = _history(connection)
    second = replay(
        connection, _options(tmp_path, "two", release, write_history=True), POLICY, report=_quiet
    )

    assert first == second
    assert _snapshots(tmp_path / "one") == _snapshots(tmp_path / "two")
    assert (tmp_path / "one" / "report.json").read_bytes() == (
        tmp_path / "two" / "report.json"
    ).read_bytes()
    assert _history(connection) == events
    assert [(e[0], e[2]) for e in events] == [
        ("jdf:A", "departure"),
        ("jdf:B", "arrival"),
        ("jdf:B", "departure"),
        ("jdf:C", "arrival"),
    ]
    assert first["running_key_groups"] == {"duk/duk": {"bound": 1, "key_groups": 2}}
    assert first["results"]["jdf/no_line"] > 0
    count = connection.execute("SELECT count(*) FROM rt.observation").fetchone()
    assert count is not None and count[0] == 160


def test_warm_restart_continues_like_an_uninterrupted_run(
    connection: psycopg.Connection, tmp_path: Path, release: Path
) -> None:
    replay(connection, _options(tmp_path, "full", release), POLICY, report=_quiet)
    restart: Instant = instant(START + timedelta(minutes=22))

    replay(
        connection,
        _options(tmp_path, "before", release, write_history=True, until=restart),
        POLICY,
        report=_quiet,
    )
    loads = run_loads(connection, "run-a")
    assert loads is not None
    runner = Runner(connection, loads, POLICY, ("jdf",))
    assert warm_start(runner, connection, restart, POLICY.warm_replay_hours) > 0
    replay(
        connection,
        _options(tmp_path, "after", release, since=restart),
        POLICY,
        runner=runner,
        report=_quiet,
    )

    full, after = _snapshots(tmp_path / "full"), _snapshots(tmp_path / "after")
    assert after
    assert {name: full[name] for name in after} == after


def test_a_mid_day_release_switch_adds_a_revision(
    connection: psycopg.Connection, tmp_path: Path, release: Path
) -> None:
    halfway = instant(START + timedelta(minutes=27))  # after B (08:10), before C
    replay(
        connection,
        _options(tmp_path, "before", release, write_history=True, until=halfway),
        POLICY,
        report=_quiet,
    )
    loads = run_loads(connection, "run-a")
    assert loads is not None
    runner = Runner(connection, loads, POLICY, ("jdf",))
    warm_start(runner, connection, halfway, POLICY.warm_replay_hours)

    second = write_release(tmp_path / "b", "run-b", jdf=serving_rows(_timetable(drop_b=True)))
    from obehy.realtime.core import CORE_VERSION
    from obehy.realtime.emit.db import Writer
    from obehy.realtime.model import Derivation

    new = ensure_release(connection, second, report=_quiet)
    runner.switch_release(
        new, Writer(connection, Derivation(CORE_VERSION, POLICY.version, "run-b"))
    )

    rows = connection.execute(
        "SELECT location_id, event_type, revision, orphaned FROM history.actual_stop_event"
        " ORDER BY 1, 2"
    ).fetchall()
    assert rows == [
        ("jdf:A", "departure", 2, False),
        ("jdf:B", "arrival", 1, True),
        ("jdf:B", "departure", 1, True),
    ]
    (instance,) = runner.runtimes["jdf"].state.instances.values()
    assert instance.release_id == "run-b"
    assert [c.location_id for c in instance.calls] == ["jdf:A", "jdf:C"]


def test_worker_polls_decodes_and_emits(
    connection: psycopg.Connection, tmp_path: Path, release: Path, database_url: str
) -> None:
    import asyncio

    from obehy.realtime.manifest import Channel
    from obehy.realtime.worker import run_worker
    from obehy.release import activate as activation
    from obehy.release.contract import load_contract

    ensure_release(connection, release, report=_quiet)
    activation.activate(connection, load_contract(), "run-a")
    now = datetime.now(UTC)
    body = json.dumps(
        {
            "VehicleList": [
                {
                    "ID": 810,
                    "Delay": 0,
                    "RouteID": 143,
                    "CISLineID": 582492,
                    "State": 0,
                    "Longitude": ORIGIN[0],
                    "Latitude": ORIGIN[1],
                }
            ]
        }
    ).encode()

    def fetch(channel: Channel) -> Poll:
        return Poll(now, now, 200, body, "application/json")

    channel = Channel("duk", "vehicles", "GET", "https://x.invalid", 15.0, 5.0)
    asyncio.run(
        run_worker(
            database_url,
            [channel],
            POLICY,
            archive=tmp_path / "live-archive",
            gtfs_rt_dir=tmp_path / "gtfs-rt",
            fetcher=fetch,
            once=True,
        )
    )
    assert (tmp_path / "gtfs-rt" / "jdf.pb").is_file()
    assert list((tmp_path / "live-archive" / "duk" / "vehicles").iterdir())
    row = connection.execute(
        "SELECT status, reason FROM rt.vehicle_state_current WHERE source_vehicle_id = '810'"
    ).fetchone()
    assert row is not None  # bound today or not in service: either way decoded and stepped
    count = connection.execute("SELECT count(*) FROM rt.observation").fetchone()
    assert count is not None and count[0] == 1
