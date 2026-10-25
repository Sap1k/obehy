"""Readable static codes: features are source codes read with their official texts (JDF 1.11
Tabulka pevných kódů, SŽ KADR číselníky), amenities and flags come from the typed kinds."""

from __future__ import annotations

import json
from typing import Any

from obehy.api import schema
from obehy.api.codes import (
    amenities,
    call_flags,
    features,
    load_codes,
    tristate,
    wheelchair_of,
)
from obehy.api.schema import Boarding
from obehy.release.contract import CONTRACT_PATH

CODES = load_codes()


def _enum(name: str) -> list[str]:
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return list(document["enumerations"][name]["values"])


def _row(kind: str, scope: str = "trip", code: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "scope": scope, "source_code": code} | extra


def test_every_kind_and_boarding_code_has_a_home() -> None:
    assert sorted(CODES.kinds) == sorted(_enum("assignment_kind"))
    assert sorted(CODES.boarding) == sorted(int(v) for v in _enum("gtfs_pickup_dropoff"))


def test_one_code_is_one_feature_with_its_official_text_and_all_its_kinds() -> None:
    # CZPTT central note 28: JrUtil types it into three kinds.
    rows = [
        _row("bicycle_transport", code="28"),
        _row("bicycle_carry_on", code="28"),
        _row("bicycle_reservation_available", code="28"),
    ]
    (feature,) = features(CODES, rows)
    assert feature.kinds == [
        "bicycle_carry_on",
        "bicycle_reservation_available",
        "bicycle_transport",
    ]
    assert feature.official and feature.source_code is not None
    assert (feature.source_code.system, feature.source_code.name) == (
        "czptt_note",
        "kolo rezervace možná",
    )
    assert feature.label.cs.startswith("přeprava spoluzavazadel s možností rezervace místa")
    assert feature.group == "bicycles"


def test_a_jdf_code_reads_its_text_for_the_trip_or_the_stop() -> None:
    (on_trip,) = features(CODES, [_row("wheelchair_accessible_vehicle", code="@")])
    (at_stop,) = features(CODES, [_row("wheelchair_accessible", "location", "@")])
    assert on_trip.label.cs == "spoj s bezbariérově přístupným vozidlem"
    assert at_stop.label.cs == "bezbariérově přístupná zastávka"
    (ordered,) = features(CODES, [_row("on_request", code="T")])
    (call,) = features(CODES, [_row("on_request", "call", "T", call_sequence=3)])
    assert ordered.label.cs == "spoj jede jen na objednání"
    assert call.label.cs == "spoj zastávku obsluhuje jen na objednání"
    (day,) = features(CODES, [_row("calendar_designation", code="X")])
    assert (day.label.cs, day.label.en) == ("jede v pracovních dnech", "runs on working days")


def test_the_strongest_statement_wins_and_partial_ones_stay_features() -> None:
    found = amenities(
        features(
            CODES,
            [
                _row("wheelchair_accessible_vehicle", code="34"),
                _row("wheelchair_booking_required", code="34"),
                _row("bicycle_transport", code="28"),
                _row("bicycle_reservation_available", code="28"),
                # A ban on some calls only does not make the whole trip bicycle-free.
                _row("bicycle_transport_prohibited", "call", "36", call_sequence=4),
                _row("calendar_designation", code="+"),
                _row("integrated_transport", "route", "30421"),
                _row("integrated_transport", "route", "30013"),
            ],
        )
    )
    assert (found.wheelchair, found.bicycles) == ("booking_required", "reservation_available")
    assert [o.cs for o in found.operates] == ["jede v neděli a ve státem uznané svátky"]
    assert found.integrated_transport == [
        schema.IntegratedSystem(code="30013", name=None, abbreviation=None),
        schema.IntegratedSystem(code="30421", name="Doprava Ústeckého kraje", abbreviation="DÚK"),
    ]


def test_call_flags() -> None:
    stated = features(
        CODES,
        [
            _row("on_request", "call", "0030", call_sequence=3),  # CZPTT: a request stop
            _row("on_request", "call", "T", call_sequence=4),  # JDF: served only on order
            _row("exit_only", "call_range", "(", call_sequence=5, call_sequence_to=7),
            _row("on_request", "trip", "T"),  # the trip runs on request: no call flag
        ],
    )

    def flags(
        sequence: int, boarding: Boarding = "regular", alighting: Boarding = "regular"
    ) -> list[str]:
        return call_flags(
            stated, sequence, boarding, alighting, first=sequence == 1, last=sequence == 9
        )

    assert flags(3) == ["request_stop"]
    assert flags(4) == ["on_request"]
    assert flags(6) == ["exit_only"]
    assert flags(8) == []
    assert flags(2, alighting="ask_driver") == ["request_stop"]
    # No alighting at the origin, no boarding at the terminus: what every trip does.
    assert flags(1, alighting="none") == [] and flags(9, boarding="none") == []
    assert flags(2, alighting="none") == ["boarding_only"]
    assert amenities(stated).on_request is True


def test_a_central_note_reads_its_official_text() -> None:
    note = CODES.note(
        {
            "note_kind": "czptt_central_note",
            "note_code": "40",
            "note_label": "WiFi",
            "note_text": "40|CZ57076||CZ73074||0|",
        }
    )
    assert (note.code, note.label, note.text) == (
        "40",
        "WiFi",
        "ve vlaku je řazen vůz s bezdrátovým připojením k internetu",
    )


def test_a_central_note_printed_for_some_categories_reads_without_them() -> None:
    (feature,) = features(CODES, [_row("note", code="12")])
    assert feature.label.cs == "ve vlaku řazeny k sezení pouze vozy 2. vozové třídy"
    assert not feature.official and feature.source_code is not None
    assert feature.source_code.text is not None
    assert feature.source_code.text.startswith("u vlaků kategorie R a vyšší")
    note = CODES.note({"note_kind": "czptt_central_note", "note_code": "44", "note_label": "IDS"})
    assert note.text == "vlak zařazený v integrovaném dopravním systému"


def test_days_of_operation_read_in_week_order() -> None:
    found = amenities(
        features(CODES, [_row("calendar_designation", code=c) for c in ("7", "+", "6", "X")])
    )
    assert [o.cs for o in found.operates] == [
        "jede v pracovních dnech",
        "jede v sobotu",
        "jede v neděli",
        "jede v neděli a ve státem uznané svátky",
    ]


def test_a_board_row_reads_the_strongest_wheelchair_statement() -> None:
    # CZPTT note 34 gives both kinds: the booking is what a passenger has to know.
    both = ["wheelchair_accessible_vehicle", "wheelchair_booking_required"]
    assert wheelchair_of(both, True) == "booking_required"
    assert wheelchair_of(["wheelchair_accessible_vehicle"], None) == "accessible"
    # No code, but the trip's GTFS flag says so.
    assert wheelchair_of([], True) == "accessible"
    assert wheelchair_of([], False) is None and wheelchair_of([], None) is None


def test_tristates_and_unknown_kinds() -> None:
    assert (tristate(None), tristate(0), tristate(1), tristate(2)) == (None, None, True, False)
    assert CODES.kind("a_future_kind").group == "other"
