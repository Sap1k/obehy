"""The index loaded lazily from PostgreSQL equals the builder's index (docs/R1_SLICE.md 7)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from obehy.realtime.index_sql import IndexLoader, active_loads, ensure_release, run_loads
from obehy.release import activate as activation
from obehy.release.contract import load_contract
from obehy.release.load import load_release
from tests.db.serving_fixture import czptt_rows, write_release
from tests.realtime.builder import Timetable, serving_rows, timetable

pytestmark = pytest.mark.postgres

CONTRACT = load_contract()
DAY = date(2026, 10, 8)


def _quiet(_: str) -> None:
    pass


def _timetable() -> Timetable:
    return (
        timetable()
        .trip("582492:143", days=[DAY], calls=[("A", "23:50"), ("B", "24:05"), ("A", "24:20")])
        .trip("582492:144", days=[DAY, date(2026, 10, 9)], calls=[("B", "06:00"), ("C", "06:20")])
        .trip("582493:1", days=[date(2026, 10, 9)], calls=[("C", "07:00"), ("D", ("07:10"))])
    )


def _load(connection: psycopg.Connection, tmp_path: Path, tt: Timetable) -> None:
    release = write_release(tmp_path, "run-rt", jdf=serving_rows(tt))
    load_release(connection, release, CONTRACT, report=_quiet)


def test_loaded_index_matches_the_builder(connection: psycopg.Connection, tmp_path: Path) -> None:
    tt = _timetable()
    _load(connection, tmp_path, tt)
    loads = run_loads(connection, "run-rt")
    assert loads is not None
    loader = IndexLoader(connection, loads, "jdf")
    expected = tt.index()
    refs = [("cis:line_trip", "582492:143"), ("cis:line_trip", "582492:144")]
    days = [DAY - date.resolution, DAY, DAY + date.resolution]

    index = loader.ensure(refs, days)

    for ref in refs:
        assert index.keys(*ref) == expected.keys(*ref)
        for entry in index.keys(*ref):
            trip = index.trip(entry.public_id)
            assert trip == expected.trip(entry.public_id)
            assert trip.shape_id is not None
            assert index.shape(trip.shape_id) == expected.shape(trip.shape_id)
            for day in days:
                assert index.runs_on(trip.service_id, day) == expected.runs_on(trip.service_id, day)
            for call in trip.calls:
                assert index.location(call.location_id) == expected.location(call.location_id)
    assert index.keys("cis:line", "582492") == expected.keys("cis:line", "582492")
    assert "jdf:t3" not in index.trips  # loaded lazily, only when asked for


def test_unknown_keys_are_remembered_and_dates_extend(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    _load(connection, tmp_path, _timetable())
    activation.activate(connection, CONTRACT, "run-rt")
    loader = IndexLoader(connection, active_loads(connection), "jdf")
    index = loader.ensure([("cis:line_trip", "999999:1"), ("cis:line_trip", "582492:9")], [DAY])
    assert index.keys("cis:line_trip", "999999:1") == ()
    assert index.keys("cis:line", "999999") == ()
    assert index.keys("cis:line_trip", "582492:9") == ()
    assert index.keys("cis:line", "582492") != ()

    index = loader.ensure([("cis:line_trip", "582492:144")], [DAY])
    (entry,) = index.keys("cis:line_trip", "582492:144")
    service = index.trip(entry.public_id).service_id
    assert index.runs_on(service, DAY)
    later = date(2026, 10, 9)
    loader.ensure([], [later])
    assert index.runs_on(service, later)


def test_runs_are_found_by_run_id(connection: psycopg.Connection, tmp_path: Path) -> None:
    assert run_loads(connection, "missing") is None
    _load(connection, tmp_path, _timetable())
    loads: Any = run_loads(connection, "run-rt")
    assert set(loads.load_ids) == {"jdf", "czptt"}


def test_ensure_release_loads_a_directory_once(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    release = write_release(tmp_path, "run-rt", jdf=serving_rows(_timetable()))
    first = ensure_release(connection, release, report=_quiet)
    assert ensure_release(connection, "run-rt", report=_quiet) == first
    assert ensure_release(connection, release, report=_quiet) == first


def test_rail_trips_load_their_run_train_numbers_and_sr70_keys(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    rows = czptt_rows()
    run_key = rows["trip"][0]["run_key"]
    station, point = "czptt:location:CZ54000", "czptt:location:CZ54001"
    track = f"{station}:platform:2"
    rows["location"].append(
        {
            "location_id": track,
            "kind": "boarding_point",
            "domain": "heavy_rail",
            "parent_location_id": station,
            "name": "Česká Lípa hl.n.",
            "public_code": "2",
            "coordinate_precision": "missing",
        }
    )
    rows["trip"].append(
        {
            "trip_id": "czptt:trip:1:2",
            "route_id": "czptt:route:1",
            "service_id": "czptt:service:1",
            "short_name": "Os 16600",
            "run_key": run_key,
            "run_part": 2,
        }
    )
    rows["trip_call"].append(
        {
            "trip_id": "czptt:trip:1:2",
            "sequence": 2,
            "location_id": point,
            "passenger_service": False,
            "scheduled_arrival": 6 * 3600 + 480,
            "scheduled_departure": 6 * 3600 + 480,
            "pickup_type": 1,
            "dropoff_type": 1,
            "timepoint": True,
        }
    )
    first, last = date(2026, 10, 5), date(2026, 10, 11)
    for kind, namespace, identifier, public_id in [
        ("trip", "czptt:train_number", "16600", "czptt:trip:1:2"),
        ("location", "sr70", "54000", station),
        ("location", "sr70", "54001", point),
        ("location", "sr70:track", "54000:2", track),
    ]:
        rows["source_key"].append(
            {
                "entity_kind": kind,
                "namespace": namespace,
                "identifier": identifier,
                "public_id": public_id,
                "valid_from": first,
                "valid_to": last,
                "binding_method": "identity",
            }
        )
    release = write_release(tmp_path, "run-rail", czptt=rows)
    load_release(connection, release, CONTRACT, report=_quiet)
    loads = run_loads(connection, "run-rail")
    assert loads is not None
    day = date(2026, 10, 7)

    index = IndexLoader(connection, loads, "czptt").ensure([("czptt:train_number", "6600")], [day])

    assert index.runs[run_key] == ("czptt:trip:1:1", "czptt:trip:1:2")
    part = index.trip("czptt:trip:1:1")
    assert (part.run_key, part.run_part, part.train_number) == (run_key, 1, "6600")
    run = index.run(part, day)
    assert [(p.trip_id, p.train_number, p.first, p.last) for p in run.parts] == [
        ("czptt:trip:1:1", "6600", 0, 1),
        ("czptt:trip:1:2", "16600", 1, 1),
    ]
    assert index.location_key(point, "sr70") == "54001"
    assert index.keyed_locations("sr70:track", "54000:2") == (track,)
