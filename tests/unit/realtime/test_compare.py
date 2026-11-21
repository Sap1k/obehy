"""`obehy rt compare`: the canonical form (`realtime/canonical.py`) and the facts level on a tiny
archive (`realtime/compare.py`), and the stream diff (`realtime/compare_check.py`)."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from obehy.realtime.archive import ArchiveWriter, Poll
from obehy.realtime.canonical import dumps, line, number, plain
from obehy.realtime.compare import compare
from obehy.realtime.compare_check import diff
from obehy.realtime.policy import load_policy
from obehy.realtime.replay import ReplayOptions, named_channels
from obehy.realtime.times import TimeError, instant, utc_text

# --- floats -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "places", "text"),
    [
        (50.0812345678, 7, "50.0812346"),
        (90.0, 3, "90"),
        (0.0, 3, "0"),
        (-0.0, 3, "0"),
        (-0.0001, 3, "0"),
        (-0.0006, 3, "-0.001"),
        (1.5, 2, "1.5"),
        (12.3456, 2, "12.35"),
        (0.125, 2, "0.12"),  # exactly halfway in binary: ties to even
        (0.375, 2, "0.38"),
        (2.675, 2, "2.67"),  # 2.67499999... in binary: correctly rounded, not 2.68
        (1e-07, 7, "0.0000001"),
        (123456789.0, 3, "123456789"),
        (-14.4, 7, "-14.4"),
    ],
)
def test_a_float_has_one_text_for_its_decimals(value: float, places: int, text: str) -> None:
    assert number(value, places) == text


def test_the_key_chooses_the_decimals_also_in_lists() -> None:
    record = {
        "lat": 50.08123456,
        "longitude": 14.123456789,
        "distance_m": 12.3456,
        "odometer": 7.891,
        "bearing": 33.33333,
        "coordinates": [1.23456789],
        "lon": [14.987654321],
    }
    assert dumps(record) == (
        '{"bearing":33.333,"coordinates":[1.235],"distance_m":12.35,'
        '"lat":50.0812346,"lon":[14.9876543],"longitude":14.1234568,"odometer":7.89}'
    )


def test_an_integer_stays_an_integer_and_an_integral_float_prints_like_one() -> None:
    assert dumps({"a": 5, "b": 5.0, "c": True, "d": None}) == '{"a":5,"b":5,"c":true,"d":null}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_a_non_finite_float_is_refused(value: float) -> None:
    with pytest.raises(ValueError):
        number(value, 3)


# --- times, strings, structure ------------------------------------------------------------------


def test_an_instant_is_utc_with_z_and_microseconds_only_when_set() -> None:
    prague = datetime.fromisoformat("2026-10-15T07:00:00+02:00")
    assert utc_text(prague) == "2026-10-15T05:00:00Z"
    assert utc_text(prague + timedelta(microseconds=5)) == "2026-10-15T05:00:00.000005Z"
    assert utc_text(datetime(2026, 3, 29, 1, 0, 0, 120000, tzinfo=UTC)) == (
        "2026-03-29T01:00:00.120000Z"
    )
    assert plain(date(2026, 10, 25)) == "2026-10-25"
    with pytest.raises(TimeError):
        utc_text(datetime(2026, 10, 15, 7))  # noqa: DTZ001 - naive on purpose


def test_strings_escape_as_json_with_utf8_kept() -> None:
    assert dumps('Česká Lípa "x"\\\n\t\x01\x7f') == '"Česká Lípa \\"x\\"\\\\\\n\\t\\u0001\x7f"'


class _Colour(Enum):
    RED = "red"


@dataclass(frozen=True, slots=True)
class _Thing:
    name: str
    at: datetime
    tags: frozenset[str]
    colour: _Colour
    pair: tuple[int, float]
    _hidden: int = 0


def test_a_dataclass_is_its_fields_with_its_type_and_no_hash_order() -> None:
    thing = _Thing(
        "a",
        instant(datetime(2026, 10, 15, 5, tzinfo=UTC)),
        frozenset({"z", "b", "m"}),
        _Colour.RED,
        (1, 2.5),
    )
    assert line({"kind": "x", "thing": thing, "set": {3, 1, 2}}) == (
        '{"kind":"x","set":[1,2,3],"thing":{"@type":"_Thing","at":"2026-10-15T05:00:00Z",'
        '"colour":"red","name":"a","pair":[1,2.5],"tags":["b","m","z"]}}\n'
    )


# --- the facts level ----------------------------------------------------------------------------

RECEIVED = datetime(2026, 10, 5, 19, 58, 2, tzinfo=UTC)


def _entry(vehicle: int, lat: float) -> dict[str, Any]:
    return {
        "ID": vehicle,
        "Delay": 2,
        "LineID": 486,
        "RouteID": 153,
        "Longitude": 13.87098,
        "Latitude": lat,
        "ArrivalDT": "1970-01-01T02:00:00+02:00",
        "CISLineID": 582486,
        "GPSPositionDT": "2026-10-05T21:57:48+02:00",
        "Azimut": 8.0,
        "State": 0,
    }


def _archive(root: Path) -> None:
    writer = ArchiveWriter(root)

    def poll(seconds: int, body: bytes | None, status: int | None, error: str | None) -> None:
        at = RECEIVED + timedelta(seconds=seconds)
        writer.append("duk", "vehicles", Poll(at, at, status, body, error=error), body)

    good = {"VehicleList": [_entry(810, 50.679471234), _entry(811, 50.5)]}
    poll(0, json.dumps(good).encode(), 200, None)
    poll(10, None, None, "timeout")
    poll(20, b"<html>busy</html>", 200, None)


def _records(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(text) for text in stream]


def _options(root: Path) -> ReplayOptions:
    return ReplayOptions(
        release="",
        archive=root,
        start=date(2026, 10, 5),
        end=date(2026, 10, 5),
        channels=named_channels([("duk", "vehicles")]),
        out=root,
    )


def test_the_facts_level_records_every_poll_in_order(tmp_path: Path) -> None:
    _archive(tmp_path / "raw")
    out = tmp_path / "facts.jsonl.gz"
    assert compare(_options(tmp_path / "raw"), "facts", load_policy(), out) == 7
    records = _records(out)
    assert [r["kind"] for r in records] == [
        "header",
        "poll",
        "facts",
        "facts",
        "poll",
        "poll",
        "payload_error",
    ]
    assert records[0]["level"] == "facts" and records[0]["channels"] == ["duk/vehicles"]
    assert records[1]["outcome"] == "decoded" and records[1]["observations"] == 2
    assert records[1]["at"] == "2026-10-05T19:58:02Z"
    facts = records[2]
    assert (facts["item"], facts["feed"], facts["observed_at"]) == (
        0,
        "jdf",
        "2026-10-05T19:57:48Z",
    )
    position = next(f for f in facts["facts"]["facts"] if f["t"] == "position")
    assert position == {"t": "position", "lat": 50.6794712, "lon": 13.87098, "bearing": 8}
    assert records[4]["outcome"] == "failed" and records[4]["error"] == "timeout"
    assert records[5]["outcome"] == "undecodable"
    assert records[6]["class"] == "PayloadError" and records[6]["message"].startswith("not JSON")
    # Deterministic bytes: the same window gives the same file.
    again = tmp_path / "again.jsonl.gz"
    compare(_options(tmp_path / "raw"), "facts", load_policy(), again)
    assert again.read_bytes() == out.read_bytes()


def test_the_diff_names_the_first_difference_by_kind(tmp_path: Path) -> None:
    _archive(tmp_path / "raw")
    reference = tmp_path / "reference.jsonl"
    compare(_options(tmp_path / "raw"), "facts", load_policy(), reference)
    lines = reference.read_text(encoding="utf-8").splitlines(keepends=True)
    candidate = tmp_path / "candidate.jsonl"
    candidate.write_text(
        "".join([*lines[:2], lines[2].replace("50.6794712", "50.6794713"), *lines[3:6]]),
        encoding="utf-8",
    )
    same, report = diff(reference, candidate)
    assert not same
    text = "\n".join(report)
    assert "payload_error: 1 / 0   <-- differs" in text
    assert "line 3 (facts) at .facts.facts[2].lat: 50.6794712 != 50.6794713" in text
    assert diff(reference, reference)[0]
    candidate.write_text(
        "".join([*lines[:6], lines[6].replace("not JSON", "bad JSON")]), encoding="utf-8"
    )
    assert not diff(reference, candidate)[0]
    assert diff(reference, candidate, ignore=["payload_error.message"])[0]
