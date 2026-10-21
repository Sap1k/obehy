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
from obehy.realtime.emit.vehicles import VehicleBook
from obehy.realtime.times import local_date
from obehy.reference import duk_register, vehicle_list
from obehy.reference.load import load_dataset
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
    # Fixed codes: X and @ on the trip, O for bicycles, T (on request) at Nový Bor, the line in
    # PID (30001), and a barrier-free stop with a toilet.
    rows["location"][0]["wheelchair_boarding"] = 1

    def assign(n: int, scope: str, kind: str, code: str, **target: Any) -> dict[str, Any]:
        return {
            "assignment_id": f"jdf:assignment:x{n}",
            "scope": scope,
            "kind": kind,
            "source_code": code,
            **target,
        }

    trip = {"trip_id": "jdf:trip:1"}
    rows["assignment"] += [
        assign(1, "trip", "calendar_designation", "X", **trip),
        assign(2, "trip", "wheelchair_accessible_vehicle", "@", **trip),
        assign(3, "trip", "bicycle_transport", "O", **trip),
        assign(4, "call", "on_request", "T", **trip, call_sequence=2),
        assign(5, "route", "integrated_transport", "30001", route_id="jdf:route:000001"),
        assign(6, "location", "wheelchair_accessible", "@", location_id="jdf:stop:1"),
        assign(7, "location", "toilet", "W", location_id="jdf:stop:1"),
    ]
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
        " 50.68, 14.54, NULL, 'run-old'),"
        # MPVnet reports bus 1001 too, by its plate.
        " ('jdf', 'mpvnet', '4U1 2345', 'running', now() - interval '50 seconds',"
        " 'cis:line_trip', '000001:1', '2026-10-06', 'jdf:trip:1', 50.6801, 14.5401, 92,"
        " 'run-a')"
    )
    connection.execute(
        "INSERT INTO rt.vehicle_attribute_observed VALUES"
        " ('mpvnet', '4U1 2345', 'licence_plate', '4U1 2345', now(), now()),"
        " ('duk', '1001', 'low_floor', 'false', now(), now()),"
        " ('duk', '1001', 'air_conditioning', 'true', now(), now())"
    )
    # Before the plate was known both reports ran trip 1 side by side: a pairing candidate.
    connection.execute(
        "INSERT INTO rt.vehicle_pairing VALUES ('duk', '1001', 'mpvnet', '4U1 2345',"
        " now() - interval '2 minutes', now() - interval '1 minute', 2, false,"
        ' \'{"feed": "jdf", "namespace": "cis:line_trip", "key": "000001:1",'
        ' "service_date": "2026-10-06"}\')'
    )
    # The registry: the DÚK seed knows 1001, our own list adds its plate.
    load_dataset(
        connection,
        duk_register.parse(
            "vhc_id,model,agency,year_of_manufacture,low_floor,contactless_payments,"
            "air_conditioning,alternate_fuel,USB_chargers\n"
            "1001,Iveco Crossway LE LINE 13M,AKV bus,2014,1,0,,0,0\n",
            "seed-1",
        ),
    )
    load_dataset(
        connection,
        vehicle_list.parse(
            "vehicle_id,licence_plate,operator\nduk:1001,4U1 2345,Arriva\n", "own-1"
        ),
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
    connection.execute(
        "UPDATE rt.trip_state_current SET sources = %s WHERE key = 'P1'",
        (
            json.dumps(
                [
                    {
                        "source": "duk",
                        "heard_at": "2026-10-06T04:09:00+00:00",
                        "position_at": "2026-10-06T04:09:00+00:00",
                        "delay_s": 600,
                        "leads_position": True,
                    },
                    {
                        "source": "pid",
                        "heard_at": "2026-10-06T06:08:30+02:00",
                        "position_at": None,
                        "delay_s": 540,
                        "leads_position": False,
                    },
                ]
            ),
        ),
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
    # That run's calls and what it did: left 2 min late, arrived 1 min late.
    for table in ("journey_call", "actual_stop_event", "journey_link"):
        connection.execute(
            f"CREATE TABLE history.{table}_test PARTITION OF history.{table}"
            " FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')"
        )
    connection.execute(
        "INSERT INTO history.journey_call VALUES"
        " ('jdf', 'cis:line_trip', '000001:1', '2026-10-06', 1, 1, 'jdf:stop:1', 0, true,"
        "  21600, 21600, 'Česká Lípa,,aut.nádr.'),"
        " ('jdf', 'cis:line_trip', '000001:1', '2026-10-06', 1, 2, 'jdf:stop:2', 0, true,"
        "  22500, 22500, 'Nový Bor,,nám.')"
    )
    connection.execute(
        "INSERT INTO history.actual_stop_event (feed, key_namespace, key, service_date,"
        " location_id, visit_n, event_type, revision, event_time, interval_lo, interval_hi,"
        " method, source, derivation_id)"
        " SELECT 'jdf', 'cis:line_trip', '000001:1', '2026-10-06', l, 0, e, 1, t, t, t,"
        " 'gps', 'duk', (SELECT min(derivation_id) FROM history.derivation)"
        " FROM (VALUES ('jdf:stop:1', 'departure', timestamptz '2026-10-06 06:02+02'),"
        "  ('jdf:stop:2', 'arrival', timestamptz '2026-10-06 06:16+02')) v (l, e, t)"
    )
    # Train 6600 continues as 6601 and continued from 6599 (neither in the release).
    connection.execute(
        "INSERT INTO history.journey_link VALUES"
        " ('czptt', 'czptt:train_number', '6600', 'czptt:train_number', '6601', '2026-10-07',"
        "  'continues_as', (SELECT min(derivation_id) FROM history.derivation)),"
        " ('czptt', 'czptt:train_number', '6599', 'czptt:train_number', '6600', '2026-10-07',"
        "  'continues_as', (SELECT min(derivation_id) FROM history.derivation))"
    )
    # What the worker does each tick: which vehicle each report is, and the profiles.
    book = VehicleBook.load(connection)
    for source, vehicle, seen in connection.execute(
        "SELECT source, source_vehicle_id, last_seen FROM rt.vehicle_state_current"
    ).fetchall():
        vehicle_id, bound_by = book.identify((source, vehicle), local_date(seen))
        connection.execute(
            "UPDATE rt.vehicle_state_current SET vehicle_id = %s, bound_by = %s"
            " WHERE source = %s AND source_vehicle_id = %s",
            (vehicle_id, bound_by, source, vehicle),
        )
    book.write(connection, [])
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
    assert [n["text"] for n in trip["notes"]] == ["jede přes Sloup"]
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
    old = _get(client, "/vehicles/duk:1002")  # older than the maximum age: no current state
    assert (old["vehicle"], [r["source"] for r in old["reports"]]) == (None, ["duk"])


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
        "/api/v1/stop-places/{location_id}/arrivals",
        "/api/v1/departures",
        "/api/v1/arrivals",
        "/api/v1/trips/by-key/{namespace}/{key}/{service_date}",
        "/api/v1/trips/{trip_id}/{service_date}",
        "/api/v1/journeys/{namespace}/{key}/history",
        "/api/v1/lines",
        "/api/v1/lines/{route_id}",
        "/api/v1/lines/{route_id}/timetable",
        "/api/v1/lines/{route_id}/trips",
        "/api/v1/lines/{route_id}/punctuality",
        "/api/v1/vehicles",
        "/api/v1/vehicles/{vehicle_id}",
        "/api/v1/debug/release",
        "/api/v1/debug/realtime/sources",
        "/api/v1/debug/realtime/unmatched",
        "/api/v1/debug/realtime/instances/{feed}/{namespace}/{key}/{service_date}",
        "/api/v1/debug/vehicles/pairings",
        "/api/v1/debug/vehicles/registry",
        "/api/v1/debug/vehicles/{vehicle_id}",
        "/api/v1/debug/static/trips/{trip_id}",
    }


def test_fixed_codes_are_readable(client: TestClient) -> None:
    trip = _get(client, "/trips/jdf:trip:1/2026-10-06")
    amenities = trip["amenities"]
    assert (amenities["wheelchair"], amenities["bicycles"]) == ("accessible", "allowed")
    assert amenities["integrated_transport"] == [
        {"code": "30001", "name": "MHD Praha", "abbreviation": "PID"}
    ]
    assert [o["cs"] for o in amenities["operates"]] == ["jede v pracovních dnech"]
    assert all(f["source_code"] is None or f["official"] for f in trip["features"])
    on_order = next(f for f in trip["features"] if "on_request" in f["kinds"])
    assert (on_order["label"]["cs"], on_order["from_sequence"]) == (
        "spoj zastávku obsluhuje jen na objednání",
        2,
    )
    # The origin takes no drop-off, as every origin: no flag. The second call is on order (T).
    first, second = trip["calls"]
    assert (first["boarding"], first["alighting"], first["flags"]) == (
        "regular",
        "none",
        [],
    )
    assert second["flags"] == ["on_request"]
    place = _get(client, "/stop-places/jdf:stop:1")
    assert place["wheelchair_boarding"] is True
    assert sorted(k for f in place["facilities"] for k in f["kinds"]) == [
        "toilet",
        "wheelchair_accessible",
    ]
    raw = _get(client, "/debug/static/trips/jdf:trip:1")["assignments"]
    assert {"X", "@", "O", "T"} <= {a["source_code"] for a in raw}


def test_stops_near_a_point_nearest_first(client: TestClient) -> None:
    near = _get(client, "/stops", near="50.68,14.54", radius=500)["stops"]
    assert [s["location_id"] for s in near] == ["jdf:stop:1", "czptt:location:CZ54000"]
    assert near[0]["distance_m"] == 0 and 300 < near[1]["distance_m"] < 500
    assert _get(client, "/stops", near="50.68,14.54", radius=100)["stops"][1:] == []
    _get(client, "/stops", 422, near="50.68,14.54", q="lipa")


def test_arrivals_never_at_the_first_call(client: TestClient) -> None:
    board = _get(client, "/stop-places/jdf:stop:2/arrivals", start="2026-10-06T06:00:00+02:00")
    assert _times(board["arrivals"]) == [
        ("jdf:trip:1", "2026-10-06T06:15:00+02:00", "2026-10-06T06:25:00+02:00", "predicted")
    ]
    first = _get(client, "/stop-places/jdf:stop:1/arrivals", start="2026-10-06T05:50:00+02:00")
    assert first["arrivals"] == []


def test_a_board_of_several_stops_pages_with_a_cursor(client: TestClient) -> None:
    stops = "czptt:location:CZ54000,czptt:location:CZ54002"
    start = "2026-10-07T06:00:00+02:00"
    page = _get(client, "/departures", stops=stops, start=start, limit=1)
    assert [s["location_id"] for s in page["stops"]] == stops.split(",")
    rows = page["departures"]
    while page["next"] is not None:
        page = _get(client, "/departures", stops=stops, start=start, limit=1, cursor=page["next"])
        rows += page["departures"]
    assert [(d["trip_id"], d["location_id"]) for d in rows] == [
        ("czptt:trip:1:1", "czptt:location:CZ54000"),
        ("czptt:trip:1:2", "czptt:location:CZ54002"),
    ]
    _get(client, "/departures", 422, stops=stops, cursor="bm90IGEgY3Vyc29y")


def test_the_vehicle_on_a_board_row_and_a_trip(client: TestClient) -> None:
    board = "/stop-places/jdf:stop:1/departures"
    (row,) = _get(client, board, start="2026-10-06T06:05:00+02:00")["departures"]
    assert row["vehicle"]["vehicle_id"] == "duk:1001"
    trip = _get(client, "/trips/jdf:trip:1/2026-10-06")
    assert [v["vehicle_id"] for v in trip["vehicles"]] == ["duk:1001"]
    assert _get(client, "/trips/jdf:trip:1/2026-10-05")["vehicles"] == []


def test_a_line_overview_timetable_and_live_trips(client: TestClient) -> None:
    (found,) = _get(client, "/lines", q="1")["lines"]
    assert found["line"]["route_id"] == "jdf:route:000001"
    detail = _get(client, "/lines/jdf:route:000001", date="2026-10-05", include="shape")
    assert (detail["runs_from"], detail["runs_until"]) == ("2026-10-05", "2026-10-10")
    assert detail["integrated_transport"] == [
        {"code": "30001", "name": "MHD Praha", "abbreviation": "PID"}
    ]
    assert [f["kinds"] for f in detail["features"]] == [["integrated_transport"]]
    assert detail["day"] == {
        "trips": 2,
        "first_departure": "2026-10-05T06:00:00+02:00",
        "last_departure": "2026-10-06T00:30:00+02:00",  # 24:30
    }
    (pattern,) = detail["patterns"]
    assert pattern["trips"] == 2 and pattern["shape"]["type"] == "LineString"
    assert (pattern["first_departure"], pattern["last_departure"]) == (
        "2026-10-05T06:00:00+02:00",
        "2026-10-06T00:30:00+02:00",
    )
    assert [s["location_id"] for s in pattern["stops"]] == ["jdf:stop:1", "jdf:stop:2"]
    (columns,) = _get(client, "/lines/jdf:route:000001/timetable", date="2026-10-05")["patterns"]
    assert [(t["trip_id"], t["times"]) for t in columns["trips"]] == [
        ("jdf:trip:1", ["2026-10-05T06:00:00+02:00", "2026-10-05T06:15:00+02:00"]),
        ("jdf:trip:2", ["2026-10-06T00:30:00+02:00", "2026-10-06T00:45:00+02:00"]),
    ]
    trips = _get(client, "/lines/jdf:route:000001/trips", date="2026-10-06")["trips"]
    assert [
        (t["trip_id"], t["lifecycle"], t["delay_s"], [v["vehicle_id"] for v in t["vehicles"]])
        for t in trips
    ] == [
        ("jdf:trip:1", "running", 600, ["duk:1001"]),
        ("jdf:trip:2", None, None, []),
    ]
    _get(client, "/lines/jdf:route:nope", 404)
    _get(client, "/lines/jdf:route:nope/timetable", 404)


def test_connections_and_journey_links_of_a_trip(client: TestClient) -> None:
    (connection,) = _get(client, "/trips/jdf:trip:1/2026-10-06")["connections"]
    assert (connection["direction"], connection["at_sequence"], connection["trip"]) == (
        "waits_for",
        1,
        None,
    )
    links = _get(client, "/trips/czptt:trip:1:1/2026-10-07")["links"]
    assert [(k["kind"], k["journey"]["key"], k["trip"]) for k in links] == [
        ("continued_from", "6599", None),
        ("continues_as", "6601", None),
    ]


def test_history_and_punctuality_from_recorded_runs(client: TestClient) -> None:
    history = _get(
        client, "/journeys/cis:line_trip/000001:1/history", since="2026-10-01", until="2026-10-07"
    )
    (day,) = history["days"]
    assert (day["service_date"], day["trip_id"]) == ("2026-10-06", "jdf:trip:old")
    assert [(c["departure_delay_s"], c["arrival_delay_s"]) for c in day["calls"]] == [
        (120, None),
        (None, 60),
    ]
    punctuality = _get(
        client, "/lines/jdf:route:000001/punctuality", since="2026-10-01", until="2026-10-07"
    )
    assert (punctuality["journeys"], punctuality["events"]) == (1, 1)
    assert punctuality["on_time_share"] == 1.0
    assert [(b["key"], b["mean_delay_s"]) for b in punctuality["by_hour"]] == [("06", 120.0)]
    _get(client, "/lines/jdf:route:000001/punctuality", 422, since="2026-01-01")


def test_debug_shows_a_journey_and_its_parts(client: TestClient) -> None:
    body = _get(client, "/debug/realtime/instances/jdf/pid:gtfs_trip_id/P1/2026-10-06")
    assert body["state"]["trip_id"] == "jdf:trip:1"
    assert [p["trip_id"] for p in body["parts"]] == ["jdf:trip:1"]
    assert [v["source"] for v in body["vehicles"]] == ["duk"]
    sources = _get(client, "/debug/realtime/sources")
    assert sources["channels"] == []
    assert [
        (c["source"], c["vehicles"], c["on_trips"], c["registered"], c["journeys"], c["leads"])
        for c in sources["current"]
    ] == [("duk", 2, 1, 1, 1, 1), ("mpvnet", 1, 1, 1, 0, 0), ("pid", 0, 0, 0, 1, 0)]
    assert _get(client, "/debug/realtime/unmatched")["groups"] == []
    release = _get(client, "/debug/release")["release"]
    assert release["run_id"] == "run-a"
    assert [p["package"] for p in release["packages"]] == ["czptt", "jdf"]
    assert [h["action"] for h in release["history"]] == ["activate"]


def test_debug_shows_why_a_vehicle_is_what_it_is(client: TestClient) -> None:
    body = _get(client, "/debug/vehicles/duk:1001")
    assert sorted((r["source"], r["bound_by"]) for r in body["reports"]) == [
        ("duk", "source"),
        ("mpvnet", "identifier"),
    ]
    assert sorted(
        (k["dataset"], k["source"] or k["scheme"], k["source_vehicle_id"] or k["identifier"])
        for k in body["keys"]
    ) == [("duk-register", "duk", "1001"), ("vehicles", "licence_plate", "4U12345")]
    assert sorted((o["source"], o["attribute"]) for o in body["observed"]) == [
        ("duk", "air_conditioning"),
        ("duk", "low_floor"),
        ("mpvnet", "licence_plate"),
    ]
    assert body["profile"]["registered"] is True
    assert [p["source_b"] for p in body["pairings"]] == ["mpvnet"]
    (pairing,) = _get(client, "/debug/vehicles/pairings", confirmed="false")["pairings"]
    assert (pairing["vehicle_a"], pairing["vehicle_b"], pairing["bound_by_b"]) == (
        "duk:1001",
        "duk:1001",
        "identifier",
    )
    assert _get(client, "/debug/vehicles/pairings", confirmed="true")["pairings"] == []
    registry = _get(client, "/debug/vehicles/registry")
    assert [(d["dataset"], d["vehicles"], d["source_keys"]) for d in registry["datasets"]] == [
        ("vehicles", 1, 0),
        ("duk-register", 1, 1),
    ]
    assert registry["datasets"][0]["identifier_keys"] == {"licence_plate": 1}
    assert registry["profiles"]["registered"] == 1
    # The register says low floor, DÚK live says not; the register's operator is not ours.
    assert registry["conflicts"] == [
        {"vehicle_id": "duk:1001", "attributes": ["low_floor", "operator"]}
    ]
    _get(client, "/debug/vehicles/nope:1", 404)


def test_one_registered_vehicle_seen_by_two_sources(client: TestClient) -> None:
    (bus,) = _get(client, "/vehicles", bbox="14.5,50.6,14.6,50.7")["vehicles"]
    assert (bus["vehicle_id"], bus["sources"], bus["source"]) == (
        "duk:1001",
        ["duk", "mpvnet"],
        "duk",  # the freshest report
    )
    detail = _get(client, "/vehicles/duk:1001")
    assert [(r["source"], r["bound_by"]) for r in detail["reports"]] == [
        ("duk", "source"),
        ("mpvnet", "identifier"),
    ]
    assert detail["registry"]["datasets"] == ["vehicles", "duk-register"]
    assert detail["registry"]["model"] == "Iveco Crossway LE LINE 13M"
    found = {a["attribute"]: a for a in detail["attributes"]}
    # Instants stored in jsonb (UTC) are answered in Prague time like every other time.
    stated = [s["observed_at"] for a in found.values() for s in a["statements"]]
    assert stated and all(t is None or t.endswith(("+01:00", "+02:00")) for t in stated)
    # The register says low floor, DÚK live says no: the registry wins, the clash shows.
    assert (found["low_floor"]["value"], found["low_floor"]["conflict"]) == ("true", True)
    assert [s["origin"] for s in found["low_floor"]["statements"]] == ["curated", "live"]
    # Our own list beats the seed on the operator, and states the plate MPVnet reports.
    assert (found["operator"]["value"], found["operator"]["source"]) == ("Arriva", "vehicles")
    assert (found["licence_plate"]["value"], found["licence_plate"]["origin"]) == (
        "4U1 2345",
        "curated",
    )
    assert found["air_conditioning"]["value"] == "true"  # only live states it
    _get(client, "/vehicles/duk:9999", 404)
    _get(client, "/vehicles/nonsense", 404)


def test_boards_and_trips_name_the_vehicle_traits_and_realtime_sources(client: TestClient) -> None:
    (row,) = _get(client, "/stop-places/jdf:stop:1/departures", start="2026-10-06T06:05:00+02:00")[
        "departures"
    ]
    assert row["sources"] == ["duk", "pid"]
    assert (row["vehicle"]["vehicle_id"], row["vehicle"]["traits"]["low_floor"]) == (
        "duk:1001",
        True,
    )
    trip = _get(client, "/trips/jdf:trip:1/2026-10-06")
    sources = trip["realtime"]["sources"]
    assert [(s["source"], s["label"]["cs"], s["leads_position"]) for s in sources] == [
        ("duk", "DÚK", True),
        ("pid", "PID", False),
    ]
    # Stored as the worker's UTC instants, answered in Prague time like every other time.
    assert (sources[0]["heard_at"], sources[0]["position_at"]) == (
        "2026-10-06T06:09:00+02:00",
        "2026-10-06T06:09:00+02:00",
    )
    assert [(v["vehicle_id"], v["sources"]) for v in trip["vehicles"]] == [
        ("duk:1001", ["duk", "mpvnet"])
    ]
