"""A railway station grouped with the stop in front of it: the pairing rules, search collapsing
and the grouped board, against a fixture release laid out around a few stations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from obehy.api.app import create_app
from obehy.release import activate as activation
from obehy.release.contract import load_contract
from obehy.release.load import load_release
from tests.db.serving_fixture import Rows, czptt_rows, jdf_rows, write_release

pytestmark = pytest.mark.postgres

CONTRACT = load_contract()
# About 100 m of latitude.
M100 = 0.0009


def _station(code: str, name: str, latitude: float) -> dict[str, Any]:
    return {
        "location_id": f"czptt:location:{code}",
        "kind": "stop_place",
        "domain": "heavy_rail",
        "name": name,
        "coordinate_precision": "exact",
        "coordinate_source": "sr70",
        "longitude": 14.6,
        "latitude": latitude,
    }


def _stop(key: str, name: str, latitude: float) -> dict[str, Any]:
    return {
        "location_id": f"jdf:stop:{key}",
        "kind": "stop_place",
        "domain": "surface",
        "name": name,
        "coordinate_precision": "exact",
        "coordinate_source": "osm",
        "longitude": 14.6,
        "latitude": latitude,
    }


def _call(trip: str, sequence: int, location: str) -> dict[str, Any]:
    return {
        "trip_id": trip,
        "sequence": sequence,
        "location_id": location,
        "passenger_service": True,
        "scheduled_arrival": 7 * 3600 + sequence * 60,
        "scheduled_departure": 7 * 3600 + sequence * 60,
        "pickup_type": 0,
        "dropoff_type": 0,
        "timepoint": True,
    }


# Ves: a plain stop nearest, the station's stop second, one too far, one never served.
# Dál: two plain stops; the nearest is the partner. Most: two stations sharing one stop.
# Pole: a station with nothing near. Lom: the station's stop is the third nearest (Brno hl.n.),
# Luh's the fourth, past the candidates.
STATIONS = [
    _station("CZ1", "Ves", 50.70),
    _station("CZ2", "Dál", 50.80),
    _station("CZ3", "Most", 50.90),
    _station("CZ4", "Most zastávka", 50.90 + 3 * M100),
    _station("CZ5", "Pole", 50.95),
    _station("CZ6", "Lom", 51.00),
    _station("CZ7", "Luh", 51.05),
]
STOPS = [
    _stop("ves1", "Ves,,náves", 50.70 + M100),
    _stop("ves2", "Ves,,žel.st.", 50.70 - 2 * M100),
    _stop("ves3", "Ves,,u pošty", 50.70 + 4.5 * M100),
    _stop("ves4", "Ves,,točna", 50.70 - 0.5 * M100),
    _stop("dal1", "Dál,,škola", 50.80 + M100),
    _stop("dal2", "Dál,,obec", 50.80 + 2 * M100),
    _stop("most", "Most,,u trati", 50.90 + 1.5 * M100),
    _stop("lom1", "Lom,,sady", 51.00 + M100),
    _stop("lom2", "Lom,,most", 51.00 - 1.5 * M100),
    _stop("lom3", "Lom,,hl.nádr.", 51.00 + 2 * M100),
    _stop("luh1", "Luh,,sady", 51.05 + M100),
    _stop("luh2", "Luh,,most", 51.05 - 1.5 * M100),
    _stop("luh3", "Luh,,park", 51.05 + 2 * M100),
    _stop("luh4", "Luh,,žel.st.", 51.05 - 2.5 * M100),
]


def _jdf() -> Rows:
    rows = jdf_rows()
    rows["location"] += STOPS
    served = [s["location_id"] for s in STOPS if s["location_id"] != "jdf:stop:ves4"]
    rows["trip_call"] += [_call("jdf:trip:1", 10 + n, stop) for n, stop in enumerate(served)]
    return rows


def _czptt() -> Rows:
    rows = czptt_rows()
    rows["location"] += STATIONS
    rows["trip_call"] += [
        _call("czptt:trip:1:1", 10 + n, s["location_id"]) for n, s in enumerate(STATIONS)
    ]
    # A replacement bus from Pole.
    rows["route"].append(
        rows["route"][0] | {"route_id": "czptt:route:nad", "mode": "bus", "gtfs_route_type": 3}
    )
    rows["trip"].append(
        rows["trip"][0]
        | {"trip_id": "czptt:trip:nad", "route_id": "czptt:route:nad", "run_key": None}
    )
    rows["trip_call"] += [
        _call("czptt:trip:nad", 1, "czptt:location:CZ5"),
        _call("czptt:trip:nad", 2, "czptt:location:CZ6"),
    ]
    return rows


@pytest.fixture
def client(
    connection: psycopg.Connection, database_url: str, tmp_path: Path
) -> Iterator[TestClient]:
    release = write_release(tmp_path, "run-a", jdf=_jdf(), czptt=_czptt())
    load_release(connection, release, CONTRACT, report=lambda _: None)
    activation.activate(connection, CONTRACT, "run-a")
    with TestClient(create_app(database_url), follow_redirects=False) as test_client:
        yield test_client


def _get(client: TestClient, path: str, **params: Any) -> Any:
    response = client.get(f"/api/v1{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _group(client: TestClient, location_id: str) -> list[str] | None:
    place = _get(client, f"/stop-places/{location_id}")
    assert [s["location_id"] for s in place["group"]] == (place["stop"]["group"] or [])
    return place["stop"]["group"]


def test_a_station_takes_its_named_stop_over_a_nearer_plain_one(client: TestClient) -> None:
    pair = ["czptt:location:CZ1", "jdf:stop:ves2"]
    assert _group(client, "czptt:location:CZ1") == pair
    # The same group from the stop's side; the plain and the far stop stand alone, and a stop
    # nobody serves is never a partner.
    assert _group(client, "jdf:stop:ves2") == pair
    for alone in ("jdf:stop:ves1", "jdf:stop:ves3", "jdf:stop:ves4"):
        assert _group(client, alone) is None


def test_without_a_named_stop_the_nearest_one_is_the_partner(client: TestClient) -> None:
    assert _group(client, "czptt:location:CZ2") == ["czptt:location:CZ2", "jdf:stop:dal1"]
    assert _group(client, "czptt:location:CZ5") is None


def test_a_named_stop_wins_among_the_few_nearest(client: TestClient) -> None:
    assert _group(client, "czptt:location:CZ6") == ["czptt:location:CZ6", "jdf:stop:lom3"]
    assert _group(client, "czptt:location:CZ7") == ["czptt:location:CZ7", "jdf:stop:luh1"]


def test_two_stations_sharing_a_stop_are_one_group(client: TestClient) -> None:
    group = ["czptt:location:CZ3", "czptt:location:CZ4", "jdf:stop:most"]
    for member in group:
        assert _group(client, member) == group


def test_stops_carry_their_modes(client: TestClient) -> None:
    stops = {s["location_id"]: s for s in _get(client, "/stops", q="ves")["stops"]}
    assert stops["jdf:stop:ves1"]["modes"] == ["bus"]
    assert stops["jdf:stop:ves4"]["modes"] == []
    # A railway's own buses are replacement buses: a station with trains shows trains.
    assert _get(client, "/stop-places/czptt:location:CZ5")["stop"]["modes"] == ["rail"]


def test_search_shows_a_group_once_as_its_station(client: TestClient) -> None:
    stops = _get(client, "/stops", q="ves")["stops"]
    ids = [s["location_id"] for s in stops]
    assert "jdf:stop:ves2" not in ids
    station = next(s for s in stops if s["location_id"] == "czptt:location:CZ1")
    assert station["name"] == "Ves"
    assert station["modes"] == ["rail", "bus"]
    assert station["group"] == ["czptt:location:CZ1", "jdf:stop:ves2"]
    # Found by the stop's name alone, the group still shows as its station.
    found = _get(client, "/stops", q="ves zel")["stops"]
    assert [s["location_id"] for s in found] == ["czptt:location:CZ1"]


def test_a_grouped_board_holds_every_member(client: TestClient) -> None:
    board = _get(client, "/stop-places/jdf:stop:ves2/departures", group="true")
    assert board["stop"]["location_id"] == "jdf:stop:ves2"
    assert [s["location_id"] for s in board["stops"]] == ["czptt:location:CZ1", "jdf:stop:ves2"]
    alone = _get(client, "/stop-places/jdf:stop:ves2/departures")
    assert [s["location_id"] for s in alone["stops"]] == ["jdf:stop:ves2"]
