"""Readable static codes: every typed kind of the serving contract has a label, and amenities
are derived from the strongest statement."""

from __future__ import annotations

import json
from typing import Any

from obehy.api import schema
from obehy.api.codes import amenities, call_flags, feature, load_codes, tristate
from obehy.release.contract import CONTRACT_PATH

CODES = load_codes()


def _enum(name: str) -> list[str]:
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return list(document["enumerations"][name]["values"])


def test_every_assignment_kind_and_boarding_code_has_a_readable_form() -> None:
    assert sorted(CODES.kinds) == sorted(_enum("assignment_kind"))
    assert sorted(CODES.boarding) == sorted(int(v) for v in _enum("gtfs_pickup_dropoff"))
    assert all(k.label.cs and k.label.en and k.icon for k in CODES.kinds.values())


def _row(kind: str, scope: str = "trip", code: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "scope": scope, "source_code": code} | extra


def test_the_strongest_statement_wins_and_partial_ones_stay_features() -> None:
    features = [
        feature(CODES, _row("wheelchair_accessible_vehicle", code="34")),
        feature(CODES, _row("wheelchair_booking_required", code="34")),
        feature(CODES, _row("bicycle_transport", code="28")),
        feature(CODES, _row("bicycle_reservation_available", code="28")),
        # A bicycle ban on some calls only does not make the whole trip bicycle-free.
        feature(CODES, _row("bicycle_transport_prohibited", "call", "36", call_sequence=4)),
        feature(CODES, _row("calendar_designation", code="+")),
        feature(CODES, _row("integrated_transport", "route", "30421")),
        feature(CODES, _row("integrated_transport", "route", "30013")),
    ]
    found = amenities(features)
    assert (found.wheelchair, found.bicycles) == ("booking_required", "reservation_available")
    assert [o.en for o in found.operates] == ["runs on Sundays and public holidays"]
    assert found.integrated_transport == [
        schema.IntegratedSystem(code="30013", name=None, abbreviation=None),
        schema.IntegratedSystem(code="30421", name="Doprava Ústeckého kraje", abbreviation="DÚK"),
    ]


def test_call_flags_cover_ranges_and_on_request_calls() -> None:
    features = [
        feature(CODES, _row("on_request", "call", "0030", call_sequence=3)),
        feature(CODES, _row("exit_only", "call_range", None, call_sequence=5, call_sequence_to=7)),
        feature(CODES, _row("on_request", "trip", "T")),  # the trip runs on request: no flag
    ]
    assert call_flags(features, 3) == ["request_stop"]
    assert call_flags(features, 6) == ["exit_only"]
    assert call_flags(features, 8) == []
    assert amenities(features).on_request is True


def test_tristates_and_unknown_kinds() -> None:
    assert (tristate(None), tristate(0), tristate(1), tristate(2)) == (None, None, True, False)
    assert CODES.kind("a_future_kind").group == "other"
