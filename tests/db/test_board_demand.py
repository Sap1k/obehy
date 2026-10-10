"""Due calls of station boards come from the release (docs/R2_SLICE.md section 7)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import psycopg
import pytest

from obehy.realtime.index_sql import run_loads
from obehy.realtime.policy import load_policy
from obehy.realtime.runtime.demand import StationDemand
from obehy.release.contract import load_contract
from obehy.release.load import load_release
from tests.db.serving_fixture import czptt_rows, write_release
from tests.realtime.observations import local

pytestmark = pytest.mark.postgres


def test_due_calls_of_stations_with_several_tracks(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    rows = czptt_rows()
    station = "czptt:location:CZ54000"
    for track in ("1", "2"):
        rows["location"].append(
            {
                "location_id": f"{station}:platform:{track}",
                "kind": "boarding_point",
                "domain": "heavy_rail",
                "parent_location_id": station,
                "name": "Česká Lípa hl.n.",
                "public_code": track,
                "coordinate_precision": "missing",
            }
        )
    second = dict(rows["trip"][0], trip_id="czptt:trip:2:1", run_key="Pa:2", run_part=1)
    rows["trip"].append(second)
    station_call = dict(rows["trip_call"][0])
    rows["trip_call"][0]["boarding_point_id"] = f"{station}:platform:1"
    rows["trip_call"].append(
        dict(station_call, trip_id="czptt:trip:2:1", boarding_point_id=f"{station}:platform:2")
    )
    rows["source_key"].append(
        {
            "entity_kind": "location",
            "namespace": "sr70",
            "identifier": "54000",
            "public_id": station,
            "valid_from": date(2026, 10, 5),
            "valid_to": date(2026, 10, 11),
            "binding_method": "identity",
        }
    )
    release = write_release(tmp_path, "run-boards", czptt=rows)
    load_release(connection, release, load_contract(), report=lambda _: None)
    loads = run_loads(connection, "run-boards")
    assert loads is not None
    demand = StationDemand(connection, loads.load_ids["czptt"], load_policy().boards)
    demand.codes = {"54000": "540005"}  # the catalogue's 6-digit code, made up for the fixture

    due = demand.due(local("2026-10-07 05:30"))

    # Both trips call at 06:06 (scheduled_departure 6 * 3600 + 360) on 2026-10-07.
    assert due == {"540005": [local("2026-10-07 06:06"), local("2026-10-07 06:06")]}
    assert demand.due(local("2026-10-07 05:31")) is due  # cached until due_refresh_s
