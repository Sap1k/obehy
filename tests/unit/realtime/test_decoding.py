"""The decoding every connector shares (`sources/decoding.py`)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from obehy.realtime.facts import Position, TripKey
from obehy.realtime.sources.decoding import (
    PayloadError,
    fix,
    fresh,
    integer,
    line_trip,
    load_json,
    number,
)
from tests.realtime.observations import local


def test_the_cis_line_and_trip_key_is_one_form_whatever_the_source_sends() -> None:
    expected = TripKey("cis:line_trip", "500459:105")
    assert line_trip(500459, 105) == expected  # DÚK: integers
    assert line_trip("500459", "105") == expected  # MPV net: text fields
    assert line_trip("500459", "0105") == expected  # a trip number with leading zeros
    assert line_trip("515202", 7) == TripKey("cis:line_trip", "515202:7")
    assert line_trip(1, 2) == TripKey("cis:line_trip", "000001:2")


def test_numbers_come_from_json_numbers_and_numeric_strings_never_booleans() -> None:
    assert number(1) == 1.0 and number(1.5) == 1.5 and number("49.5") == 49.5
    assert number(True) is None and number("x") is None and number(None) is None
    assert integer(3) == 3 and integer("-3") == -3
    assert integer(3.5) is None and integer(False) is None and integer("3.5") is None


def test_a_source_time_beyond_the_clock_skew_is_dropped() -> None:
    received = local("2026-10-08 08:00")
    skew = timedelta(minutes=5)
    assert fresh(local("2026-10-08 07:56"), received, skew) == local("2026-10-08 07:56")
    assert fresh(local("2026-10-08 07:50"), received, skew) is None
    assert fresh(None, received, skew) is None


def test_a_position_needs_both_coordinates_neither_zero() -> None:
    assert fix(50.1, 14.4, 90.0) == Position(lat=50.1, lon=14.4, bearing=90.0)
    assert fix(0.0, 14.4) is None and fix(50.1, 0.0) is None and fix(None, 14.4) is None


def test_a_body_that_is_not_json_is_a_payload_error() -> None:
    assert load_json(b'{"a": 1}') == {"a": 1}
    with pytest.raises(PayloadError, match="not JSON"):
        load_json(b"<html>")
