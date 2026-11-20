"""A trip's notes grouped for reading (`codes.note_groups`). Ported from the web's tests when
the grouping moved here."""

from __future__ import annotations

from typing import Any

from obehy.api import schema
from obehy.api.codes import load_codes, note_groups

CODES = load_codes()


def feature(system: str | None, code: str | None, text: str, **extra: Any) -> schema.Feature:
    fields: dict[str, Any] = {
        "kinds": ["note"], "group": "note", "icon": "note", "label": {"cs": text, "en": text},
        "official": True, "scope": "trip", "from_sequence": None, "to_sequence": None,
        "source_code": {"system": system, "code": code, "name": None, "text": text}
        if system and code else None,
    }  # fmt: skip
    return schema.Feature.model_validate(fields | extra)


def day(code: str, text: str) -> schema.Feature:
    return feature("jdf", code, text, kinds=["calendar_designation"], group="calendar")


def marked(*features: schema.Feature) -> list[tuple[str, str]]:
    return [(n.key, n.tier) for n in note_groups(CODES, features).marked]


def test_central_notes_once_each_by_their_text_in_the_kadr_print_order() -> None:
    wifi = "ve vlaku je řazen vůz s bezdrátovým připojením k internetu"
    groups = note_groups(
        CODES,
        [
            feature("czptt_note", "44", "vlak zařazený v integrovaném dopravním systému"),
            feature("czptt_note", "40", wifi),
            feature("czptt_note", "12", "ve vlaku řazeny k sezení pouze vozy 2. vozové třídy"),
            feature("czptt_note", "40", wifi, scope="call_range", from_sequence=3, to_sequence=5),
        ],
    )
    assert [n.key for n in groups.marked] == ["czptt_note:12", "czptt_note:40", "czptt_note:44"]
    assert "dvojka" not in groups.model_dump_json()


def test_a_central_note_of_some_calls_is_signed_by_its_note_code() -> None:
    text = "ve vlaku je řazen vůz s přípojkou 230 V"
    note = {"kind": "czptt_central_note", "code": "30", "label": "zásuvka", "text": text}
    groups = note_groups(CODES, [feature(None, None, text, scope="call_range", note=note)])
    assert [(n.key, n.text, n.tier) for n in groups.marked] == [("czptt_note:30", text, "plain")]


def test_plain_signs_alone_stated_ones_with_text_details_for_later() -> None:
    assert marked(
        feature("czptt_note", "40", "wifi"),
        feature("czptt_note", "33", "vůz pro cestující s dětmi"),
        feature("czptt_note", "32", "náhradní autobusová doprava"),
    ) == [("czptt_note:33", "detail"), ("czptt_note:40", "plain"), ("czptt_note:32", "stated")]
    # A wheelchair booking is a badge too.
    assert marked(feature("czptt_note", "34", "vozík")) == [("czptt_note:34", "plain")]


def test_the_days_of_operation_read_in_week_order() -> None:
    groups = note_groups(
        CODES,
        [
            day("7", "jede v neděli"),
            day("+", "jede v neděli a ve státem uznané svátky"),
            day("6", "jede v sobotu"),
            day("X", "jede v pracovních dnech"),
        ],
    )
    assert [n.key for n in groups.operates] == ["jdf:X", "jdf:6", "jdf:7", "jdf:+"]


def test_a_calls_stopping_codes_are_left_to_the_timetable() -> None:
    request = feature(
        "jdf", "x", "na znamení", kinds=["request_stop"], scope="call", from_sequence=4
    )
    bikes = feature("jdf", "O", "spoj přepravuje jízdní kola", kinds=["bicycle_transport"])
    assert marked(request, bikes) == [("jdf:O", "plain")]


def test_a_code_without_a_sign_goes_last_as_detail() -> None:
    assert marked(
        feature("czptt_note", "99", "nová"), feature("czptt_note", "22", "zavazadla")
    ) == [
        ("czptt_note:22", "plain"),
        ("czptt_note:99", "detail"),
    ]


def test_the_longer_texts_stay_apart_from_the_days_of_operation() -> None:
    route = {"kind": "route_information", "label": None, "text": "Grafikony: PD T3719"}
    calendar = {"kind": "calendar", "label": None, "text": "jede v pracovních dnech"}
    groups = note_groups(
        CODES,
        [
            day("X", "jede v pracovních dnech"),
            feature(None, None, "Grafikony: PD T3719", note=route),
            feature(None, None, "Grafikony: PD T3719", note=route),
            feature(None, None, "jede v pracovních dnech", note=calendar),
        ],
    )
    assert [n.text for n in groups.operates] == ["jede v pracovních dnech"]
    assert (groups.marked, groups.long) == ([], ["Grafikony: PD T3719"])
