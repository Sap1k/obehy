"""SŽ train map connector (docs/sources/sz.md quirk ledger)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from pyproj import Transformer

from obehy.realtime.model import (
    Delay,
    Interval,
    NextPoint,
    NextStopPrediction,
    PointEvent,
    Position,
    ServiceDay,
    TripKey,
    TripStatus,
    VehicleKey,
)
from obehy.realtime.sources import sz
from tests.realtime.observations import local

RECEIVED = local("2026-10-05 23:59:31")
SKEW = timedelta(minutes=20)
TO_KROVAK = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)


def _feature(**overrides: Any) -> dict[str, Any]:
    x, y = TO_KROVAK.transform(14.53, 50.68)
    properties: dict[str, Any] = {
        "id": "TR/3189/KASO---57501/00/2026/20261005",
        "type": "V",
        "a": 220.5,
        "tt": "Os",
        "tn": "16001",
        "cna": "Česká Lípa hl.n.",
        "rr": 0,
        "cp": "23:50",
        "cr": "23:57",
        "de": 7,
        "pde": "6 min",
        "nna": "AHr Skalice u Č.L. z",
        "zst_sr70": "567990",
        "nsn": "Mimoň",
        "nsn70": "56901",
        "nst": "00:10",
        "nsp": "00:16",
        "s": 0,
        "di": 0,
        "e": 0,
    }
    properties.update(overrides)
    return {
        "type": "Feature",
        "id": properties["id"],
        "geometry": {"type": "Point", "coordinates": [x, y]},
        "properties": properties,
    }


def _decode(*features: dict[str, Any], md: str = "05.10.2026 23:59:30") -> list[Any]:
    body = json.dumps({"md": md, "success": True, "result": list(features)}).encode()
    return sz.decode(body, "a" * 64, RECEIVED, SKEW)


def test_keys_service_day_and_vehicle() -> None:
    (obs,) = _decode(_feature())
    assert obs.feed == "czptt" and obs.observed_at == local("2026-10-05 23:59:30")
    assert obs.first(VehicleKey) == VehicleKey("TR/3189/KASO---57501/00/2026/20261005")
    assert obs.all(TripKey) == (
        TripKey("czptt:tr", "Tr:3189:KASO---57501:00:2026"),
        TripKey("czptt:train_number", "16001"),  # SZ-Q5: only a fallback
    )
    assert obs.first(ServiceDay) == ServiceDay(date(2026, 10, 5))
    assert obs.first(Delay) == Delay(420, "point")
    assert obs.first(TripStatus) == TripStatus(False, False)


def test_sz_q1_positions_are_krovak() -> None:
    (obs,) = _decode(_feature())
    position = obs.first(Position)
    assert position is not None
    assert abs(position.lon - 14.53) < 1e-5 and abs(position.lat - 50.68) < 1e-5


def test_sz_q2_bare_times_resolve_around_the_response_time() -> None:
    (obs,) = _decode(_feature(cr="00:01", cp="23:58"))
    point = obs.first(PointEvent)
    assert point is not None
    assert point.actual == Interval(local("2026-10-06 00:01"), local("2026-10-06 00:01:59"))
    assert point.scheduled == local("2026-10-05 23:58")
    prediction = obs.first(NextStopPrediction)
    assert prediction == NextStopPrediction(
        "56901", local("2026-10-06 00:10"), local("2026-10-06 00:16")
    )


def test_sz_q2_a_response_time_beyond_the_skew_is_not_the_observation_time() -> None:
    (obs,) = _decode(_feature(), md="05.10.2026 22:00:00")
    assert obs.observed_at is None


def test_sz_q3_standing_is_kept() -> None:
    standing, moving = _decode(_feature(rr=1), _feature(id="TR/1/X/00/2026/20261005"))
    assert standing.first(PointEvent).standing  # type: ignore[union-attr]
    assert not moving.first(PointEvent).standing  # type: ignore[union-attr]


def test_sz_q4_names_give_every_catalogue_code_and_codes_lose_the_check_digit() -> None:
    (obs,) = _decode(_feature(cna="Brno hl.n."))
    point = obs.first(PointEvent)
    assert point is not None and len(point.codes) > 1  # one name, several codes
    assert all(len(code) == 5 for code in point.codes)
    assert obs.first(NextPoint) == NextPoint("AHr Skalice u Č.L. z", "56799")


def test_sz_q7_a_next_point_without_a_code() -> None:
    (obs,) = _decode(_feature(nna="vl. v km 2,847", zst_sr70=None))
    assert obs.first(NextPoint) == NextPoint("vl. v km 2,847", None)


def test_sz_q8_no_bearing_while_standing() -> None:
    (obs,) = _decode(_feature(a=""))
    position = obs.first(Position)
    assert position is not None and position.bearing is None


def test_entries_without_an_identity_are_dropped() -> None:
    assert _decode(_feature(id="nonsense")) == []
