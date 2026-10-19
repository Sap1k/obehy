"""The project API against a loaded and activated fixture release with realtime state rows."""

from __future__ import annotations

import json
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
RUN = "Pa:0054:KT----06600A:00:2026"


def _jdf() -> Rows:
    rows = jdf_rows()
    # Passengers may board at the last call too: it is still not a departure.
    rows["trip_call"][1]["pickup_type"] = 0
    rows["trip"].append(
        {"trip_id": "jdf:trip:2", "route_id": "jdf:route:000001", "service_id": "jdf:service:1"}
    )
    for sequence, location, seconds in ((1, "jdf:stop:1", 88200), (2, "jdf:stop:2", 89100)):
        rows["trip_call"].append(
            {
                "trip_id": "jdf:trip:2",
                "sequence": sequence,
                "location_id": location,
                "passenger_service": True,
                "scheduled_arrival": seconds,
                "scheduled_departure": seconds,
                "pickup_type": 0,
                "dropoff_type": 0,
                "timepoint": True,
            }
        )
    rows["source_key"].append(
        {
            **rows["source_key"][0],
            "namespace": "pid:gtfs_trip_id",
            "identifier": "P1",
            "binding_method": "regional_overlay",
        }
    )
    return rows


def _czptt() -> Rows:
    """Train 6600 on the 7th as a run of two parts: Česká Lípa hl.n. ~ Zákupy | Zákupy Mimoň."""

    rows = czptt_rows()
    station = dict(rows["location"][0])
    for code, name, lon in (("CZ54002", "Zákupy", 14.64), ("CZ54003", "Mimoň", 14.72)):
        rows["location"].append(
            station | {"location_id": f"czptt:location:{code}", "name": name, "longitude": lon}
        )

    def call(trip: str, sequence: int, code: str, arrival: int, departure: int) -> dict[str, Any]:
        return {
            "trip_id": trip,
            "sequence": sequence,
            "location_id": f"czptt:location:{code}",
            "passenger_service": True,
            "scheduled_arrival": arrival,
            "scheduled_departure": departure,
            "pickup_type": 0,
            "dropoff_type": 0,
            "timepoint": True,
        }

    first, second = "czptt:trip:1:1", "czptt:trip:1:2"
    rows["trip_call"].append(call(first, 3, "CZ54002", 6 * 3600 + 900, 6 * 3600 + 960))
    rows["trip"].append(rows["trip"][0] | {"trip_id": second, "run_key": RUN, "run_part": 2})
    rows["trip"][0]["run_key"] = RUN
    rows["trip_call"] += [
        call(second, 3, "CZ54002", 6 * 3600 + 900, 6 * 3600 + 960),
        call(second, 4, "CZ54003", 6 * 3600 + 1800, 6 * 3600 + 1800),
    ]
    rows["source_key"].append(rows["source_key"][0] | {"public_id": second})
    return rows


def _call(sequence: int, location: str, status: str, at: str | None) -> dict[str, Any]:
    stamp = None if at is None else f"{at}+02:00"
    return {
        "sequence": sequence,
        "location_id": location,
        "visit_n": 0,
        "status": status,
        "estimated_arrival": stamp,
        "estimated_departure": stamp,
        "source_class": "gps" if status != "scheduled" else None,
        "passenger": True,
        "platform": None,
    }


def _state(
    connection: psycopg.Connection,
    journey: tuple[str, str, str, str],
    trip_id: str,
    calls: list[dict[str, Any]],
    parts: list[tuple[str, int, int]],
    release: str = "run-a",
) -> None:
    connection.execute(
        "INSERT INTO rt.trip_state_current VALUES"
        " (%s, %s, %s, %s, %s, %s, 'running', 600, false, false, now(), %s)",
        (*journey, release, trip_id, json.dumps(calls)),
    )
    for part, first, last in parts:
        connection.execute(
            "INSERT INTO rt.trip_part_current VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (*journey, part, first, last, release),
        )


def _vehicles(connection: psycopg.Connection) -> None:
    """A fresh bus on trip 1, one an hour old, an unmatched one, one of another release."""

    connection.execute(
        "INSERT INTO rt.vehicle_state_current (feed, source, source_vehicle_id, status, last_seen,"
        " key_namespace, key, service_date, trip_id, latitude, longitude, bearing, release_id)"
        " VALUES ('jdf', 'duk', '1001', 'running', now() - interval '30 seconds',"
        " 'pid:gtfs_trip_id', 'P1', '2026-10-06', 'jdf:trip:1', 50.68, 14.54, 90, 'run-a'),"
        " ('jdf', 'duk', '1002', 'running', now() - interval '1 hour', NULL, NULL, NULL, NULL,"
        " 50.68, 14.54, NULL, 'run-a'),"
        " ('jdf', 'duk', '1003', 'unmatched', now(), NULL, NULL, NULL, NULL,"
        " 50.681, 14.541, NULL, 'run-a'),"
        " ('czptt', 'sz', '7', 'running', now(), NULL, NULL, NULL, NULL,"
        " 50.68, 14.54, NULL, 'run-old')"
    )


@pytest.fixture
def client(
    connection: psycopg.Connection, database_url: str, tmp_path: Path
) -> Iterator[TestClient]:
    release = write_release(tmp_path, "run-a", jdf=_jdf(), czptt=_czptt())
    load_release(connection, release, CONTRACT, report=lambda _: None)
    activation.activate(connection, CONTRACT, "run-a")

    stop = "jdf:stop:1"
    pid = ("jdf", "pid:gtfs_trip_id", "P1", "2026-10-06")
    _state(
        connection,
        pid,
        "jdf:trip:1",
        [
            _call(1, stop, "predicted", "2026-10-06T06:10:00"),
            _call(2, "jdf:stop:2", "predicted", "2026-10-06T06:25:00"),
        ],
        [("jdf:trip:1", 0, 1)],
    )
    # The state of an older release is never shown with this one.
    _state(
        connection,
        ("jdf", "pid:gtfs_trip_id", "P1", "2026-10-08"),
        "jdf:trip:1",
        [_call(1, stop, "predicted", "2026-10-08T06:10:00"), _call(2, stop, "scheduled", None)],
        [("jdf:trip:1", 0, 1)],
        release="run-old",
    )
    # The rail run: one journey over both parts, its own trip ID; the junction is one call.
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
    _vehicles(connection)
    # A trip ID of an earlier timetable version, kept by history.
    connection.execute(
        "CREATE TABLE history.journey_schedule_test PARTITION OF history.journey_schedule"
        " FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')"
    )
    connection.execute(
        "INSERT INTO history.derivation (core_version, policy_version, release_id)"
        " VALUES ('c', 'p', 'run-old')"
    )
    connection.execute(
        "INSERT INTO history.journey_schedule VALUES ('jdf', 'cis:line_trip', '000001:1',"
        " '2026-10-06', 1, 'run-old', 'jdf:trip:old', '1', 'Nový Bor',"
        " (SELECT min(derivation_id) FROM history.derivation))"
    )
    with TestClient(create_app(database_url), follow_redirects=False) as test_client:
        yield test_client


def _get(client: TestClient, path: str, status: int = 200, **params: Any) -> Any:
    """GET `/api/v1<path>`; `start` is sent as `from`."""

    if "start" in params:
        params["from"] = params.pop("start")
    response = client.get(f"/api/v1{path}", params=params)
    assert response.status_code == status, response.text
    return response.json()


def _times(departures: list[dict[str, Any]]) -> list[tuple[str, str, str | None, str]]:
    return [(d["trip_id"], d["scheduled"], d["estimated"], d["status"]) for d in departures]


def test_status_names_the_active_release(client: TestClient) -> None:
    body = _get(client, "/status")
    assert (body["release_id"], body["api_version"], body["sources"]) == ("run-a", "1", [])


def test_stop_search_ignores_case_and_diacritics(client: TestClient) -> None:
    names = [s["name"] for s in _get(client, "/stops", q="ceska LIPA")["stops"]]
    assert sorted(names) == ["Česká Lípa hl.n.", "Česká Lípa,,aut.nádr."]
    assert [s["name"] for s in _get(client, "/stops", q="lipa aut")["stops"]] == [
        "Česká Lípa,,aut.nádr."
    ]
    assert [s["name"] for s in _get(client, "/stops", q="zakupy")["stops"]] == ["Zákupy"]
    problem = _get(client, "/stops", 422, q="x")
    assert problem["status"] == 422 and "letters" in problem["detail"]


def test_stops_in_a_viewport_and_its_cap(client: TestClient) -> None:
    stops = _get(client, "/stops", bbox="14.5,50.6,14.6,50.7")["stops"]
    # The railway point is not a stop; Nový Bor has no coordinates.
    assert [s["location_id"] for s in stops] == ["czptt:location:CZ54000", "jdf:stop:1"]
    response = client.get("/api/v1/stops", params={"bbox": "12,48,19,51"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"


def test_a_stop_place_with_its_posts_lines_and_release_etag(client: TestClient) -> None:
    response = client.get("/api/v1/stop-places/jdf:stop:1:post:1")
    body = response.json()
    assert body["stop"]["location_id"] == "jdf:stop:1"
    assert [(b["location_id"], b["public_code"]) for b in body["boarding_points"]] == [
        ("jdf:stop:1:post:1", "1")
    ]
    assert [line["short_name"] for line in body["lines"]] == ["1"]
    assert response.headers["etag"] == '"run-a"'
    again = client.get("/api/v1/stop-places/jdf:stop:1", headers={"If-None-Match": '"run-a"'})
    assert again.status_code == 304
    _get(client, "/stop-places/czptt:location:CZ54001", 404)  # a railway point


def test_departures_scheduled_and_late(client: TestClient) -> None:
    board = "/stop-places/jdf:stop:1/departures"
    monday = _get(client, board, start="2026-10-05T05:50:00+02:00")["departures"]
    assert _times(monday) == [("jdf:trip:1", "2026-10-05T06:00:00+02:00", None, "scheduled")]
    assert monday[0]["platform"] == "1" and monday[0]["line"]["short_name"] == "1"
    # Scheduled 06:00, five minutes ago, but expected at 06:10: still on the board.
    late = _get(client, board, start="2026-10-06T06:05:00+02:00")["departures"]
    assert _times(late) == [
        ("jdf:trip:1", "2026-10-06T06:00:00+02:00", "2026-10-06T06:10:00+02:00", "predicted")
    ]
    assert (late[0]["delay_s"], late[0]["source_class"], late[0]["stale"]) == (600, "gps", False)
    # Wednesday the 7th has no service; the 8th's state belongs to another release.
    assert _get(client, board, start="2026-10-07T05:50:00+02:00")["departures"] == []
    thursday = _get(client, board, start="2026-10-08T05:50:00+02:00")["departures"]
    assert _times(thursday) == [("jdf:trip:1", "2026-10-08T06:00:00+02:00", None, "scheduled")]
    _get(client, board, 422, start="2026-10-08T05:50:00")  # no offset


def test_departures_past_midnight_and_never_the_last_call(client: TestClient) -> None:
    night = _get(
        client,
        "/stop-places/jdf:stop:1/departures",
        minutes=30,
        start="2026-10-06T00:20:00+02:00",
    )["departures"]
    assert [(d["trip_id"], d["service_date"], d["scheduled"]) for d in night] == [
        ("jdf:trip:2", "2026-10-05", "2026-10-06T00:30:00+02:00")
    ]
    last = _get(client, "/stop-places/jdf:stop:2/departures", start="2026-10-05T00:00:00+02:00")
    assert last["departures"] == []


def test_a_rail_part_departs_with_the_run_realtime(client: TestClient) -> None:
    # Zákupy ends part 1 and starts part 2: only part 2 departs, with the run's estimate.
    board = _get(
        client,
        "/stop-places/czptt:location:CZ54002/departures",
        start="2026-10-07T06:10:00+02:00",
    )["departures"]
    assert _times(board) == [
        ("czptt:trip:1:2", "2026-10-07T06:16:00+02:00", "2026-10-07T06:19:00+02:00", "predicted")
    ]
    assert board[0]["delay_s"] == 180


def test_a_trip_with_calls_realtime_features_and_journey_key(client: TestClient) -> None:
    trip = _get(client, "/trips/jdf:trip:1/2026-10-06", include="shape")
    assert trip["journey"] == {"namespace": "cis:line_trip", "key": "000001:1"}
    assert [(c["sequence"], c["estimated_departure"], c["status"]) for c in trip["calls"]] == [
        (1, "2026-10-06T06:10:00+02:00", "predicted"),
        (2, "2026-10-06T06:25:00+02:00", "predicted"),
    ]
    assert trip["calls"][0]["scheduled_departure"] == "2026-10-06T06:00:00+02:00"
    assert trip["realtime"]["delay_s"] == 600
    assert [(f["kind"], f["note"]["text"]) for f in trip["features"]] == [
        ("note", "jede přes Sloup")
    ]
    assert trip["run_parts"] == [{"trip_id": "jdf:trip:1", "run_part": None}]
    assert trip["shape"]["type"] == "LineString"
    assert _get(client, "/trips/jdf:trip:1/2026-10-07", 404)["status"] == 404


def test_a_rail_part_shows_its_slice_of_the_run(client: TestClient) -> None:
    trip = _get(client, "/trips/czptt:trip:1:2/2026-10-07")
    assert trip["journey"] == {"namespace": "czptt:train_number", "key": "6600"}
    assert [(c["sequence"], c["estimated_arrival"]) for c in trip["calls"]] == [
        (3, "2026-10-07T06:19:00+02:00"),
        (4, "2026-10-07T06:33:00+02:00"),
    ]
    assert [p["trip_id"] for p in trip["run_parts"]] == ["czptt:trip:1:1", "czptt:trip:1:2"]
    first = _get(client, "/trips/czptt:trip:1:1/2026-10-07")
    # The railway point is not a public call.
    assert [(c["sequence"], c["status"]) for c in first["calls"]] == [
        (1, "actual"),
        (3, "predicted"),
    ]


def test_source_keys_redirect_to_the_trip(client: TestClient) -> None:
    for namespace, key, day, target in (
        ("pid:gtfs_trip_id", "P1", "2026-10-06", "/api/v1/trips/jdf:trip:1/2026-10-06"),
        ("cis:line_trip", "000001:1", "2026-10-06", "/api/v1/trips/jdf:trip:1/2026-10-06"),
        ("czptt:train_number", "6600", "2026-10-07", "/api/v1/trips/czptt:trip:1:1/2026-10-07"),
    ):
        response = client.get(f"/api/v1/trips/by-key/{namespace}/{key}/{day}")
        assert (response.status_code, response.headers["location"]) == (308, target)
    _get(client, "/trips/by-key/cis:line_trip/000001:1/2026-10-07", 404)


def test_a_trip_id_of_an_older_timetable_is_gone_with_its_key(client: TestClient) -> None:
    gone = _get(client, "/trips/jdf:trip:old/2026-10-06", 410)
    assert gone["journey"] == {"namespace": "cis:line_trip", "key": "000001:1"}
    assert gone["by_key"] == "/api/v1/trips/by-key/cis:line_trip/000001:1/2026-10-06"
    assert client.get(gone["by_key"]).headers["location"] == "/api/v1/trips/jdf:trip:1/2026-10-06"
    _get(client, "/trips/jdf:trip:never/2026-10-06", 404)


def test_vehicles_fresh_in_the_viewport_of_this_release(client: TestClient) -> None:
    box = "14.5,50.6,14.6,50.7"
    (bus,) = _get(client, "/vehicles", bbox=box)["vehicles"]
    assert (bus["vehicle_id"], bus["trip_id"], bus["delay_s"]) == ("duk:1001", "jdf:trip:1", 600)
    assert bus["line"]["short_name"] == "1" and bus["headsign"] == "Nový Bor"
    shown = _get(client, "/vehicles", bbox=box, include="unmatched")["vehicles"]
    assert [(v["vehicle_id"], v["state"], v["line"]) for v in shown][1:] == [
        ("duk:1003", "unmatched", None)
    ]
    _get(client, "/vehicles", 422, bbox=box, include="parked")
    assert _get(client, "/vehicles/duk:1001")["vehicle"]["state"] == "running"
    _get(client, "/vehicles/duk:1002", 404)  # older than the maximum age


def test_no_active_release_is_a_503(database_url: str) -> None:
    with TestClient(create_app(database_url)) as test_client:
        response = test_client.get("/api/v1/status")
    assert response.status_code == 503


def test_the_openapi_document_names_every_endpoint(database_url: str) -> None:
    with TestClient(create_app(database_url)) as test_client:
        paths = set(test_client.get("/api/v1/openapi.json").json()["paths"])
    assert paths == {
        "/api/v1/status",
        "/api/v1/stops",
        "/api/v1/stop-places/{location_id}",
        "/api/v1/stop-places/{location_id}/departures",
        "/api/v1/trips/by-key/{namespace}/{key}/{service_date}",
        "/api/v1/trips/{trip_id}/{service_date}",
        "/api/v1/vehicles",
        "/api/v1/vehicles/{vehicle_id}",
    }
