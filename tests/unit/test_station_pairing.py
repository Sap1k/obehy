"""The station pairing's policy and the curated station pairs' file."""

from __future__ import annotations

import pytest

from obehy.reference.station_pairs import parse
from obehy.release.pairing import load_pairing_policy, station_word

HEADER = "station_id,stop_id,decision,note\n"


def test_station_words_match_as_the_pairing_splits_names() -> None:
    assert station_word("hl.nadr.") == "hl nadr"
    assert station_word(" zel. st. ") == "zel st"
    policy = load_pairing_policy()
    assert "zel st" in policy.station_words
    assert policy.candidates >= 2 and policy.margin_m > 0


def test_station_pairs_parse_pairs_and_apart() -> None:
    pairs = parse(
        HEADER
        + "czptt:stop:CZ:1,jdf:stop:2,pair,at the building\n"
        + "czptt:stop:CZ:1,jdf:stop:3,apart,\n"
        + "czptt:stop:CZ:4,,apart,a halt in the fields\n",
        "v1",
    )
    assert [(p.station_id, p.stop_id, p.decision) for p in pairs.pairs] == [
        ("czptt:stop:CZ:1", "jdf:stop:2", "pair"),
        ("czptt:stop:CZ:1", "jdf:stop:3", "apart"),
        ("czptt:stop:CZ:4", "", "apart"),
    ]
    assert pairs.pairs[2].note == "a halt in the fields"


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ("jdf:stop:1,jdf:stop:2,pair,\n", "not a railway stop"),
        ("czptt:stop:CZ:1,czptt:stop:CZ:2,pair,\n", "not a surface stop"),
        ("czptt:stop:CZ:1,jdf:stop:2,maybe,\n", "not pair or apart"),
        ("czptt:stop:CZ:1,,pair,\n", "pair needs a stop_id"),
        ("czptt:stop:CZ:1,jdf:stop:2,pair,\nczptt:stop:CZ:1,jdf:stop:3,pair,\n", "already paired"),
        ("czptt:stop:CZ:1,jdf:stop:2,apart,\nczptt:stop:CZ:1,jdf:stop:2,apart,\n", "twice"),
    ],
)
def test_station_pairs_refuse_what_cannot_be_meant(rows: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse(HEADER + rows, "v1")
