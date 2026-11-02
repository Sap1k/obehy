"""MOTIS trip and stop IDs (`obehy.api.motis`) and the leg span of an enriched trip."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from obehy.api.journeys import leg_span
from obehy.api.motis import Dataset, load_datasets, parse_stop_id, parse_trip_id
from obehy.api.schema import TripCall

KNOWN = (
    Dataset("cz-JDF-merged", "trip_id"),
    Dataset("cz-PID", "source_key", "pid:gtfs_trip_id", "pid:gtfs_stop_id"),
    Dataset("jdf", "trip_id"),
)


def test_a_transitous_rail_trip_keeps_its_percent_signs_and_colons() -> None:
    trip = parse_trip_id(
        "20261103_06:47_cz-CZPTT_czptt:trip:Pa%3A0054%3AKT----11435A%3A00%3A2026:1",
        (Dataset("cz-CZPTT", "trip_id"),),
    )
    assert trip is not None
    assert (trip.service_date, trip.start, trip.trip_id) == (
        date(2026, 11, 3),
        "06:47",
        "czptt:trip:Pa%3A0054%3AKT----11435A%3A00%3A2026:1",
    )


def test_the_date_is_the_service_date_and_the_time_may_pass_midnight() -> None:
    trip = parse_trip_id("20261103_24:01_jdf_jdf:trip:1", KNOWN)
    assert trip is not None and (trip.service_date, trip.start) == (date(2026, 11, 3), "24:01")


def test_a_trip_id_with_underscores_is_split_on_the_tag_not_on_underscores() -> None:
    trip = parse_trip_id("20261103_08:55_cz-PID_991_11809_260831", KNOWN)
    assert trip is not None and trip.dataset is not None
    assert (trip.dataset.tag, trip.trip_id) == ("cz-PID", "991_11809_260831")


def test_an_unknown_dataset_is_kept_apart_from_a_malformed_id() -> None:
    trip = parse_trip_id("20261103_09:14_de-DELFI_3461130415", KNOWN)
    assert trip is not None and trip.dataset is None
    for value in ("", "jdf:trip:1", "20261103_jdf_jdf:trip:1", "20261303_06:00_jdf_x", "x_y"):
        assert parse_trip_id(value, KNOWN) is None, value


def test_stops_carry_their_dataset() -> None:
    stop = parse_stop_id("cz-PID_U476Z102P", KNOWN)
    assert stop is not None and (stop.dataset.tag, stop.stop_id) == ("cz-PID", "U476Z102P")
    assert parse_stop_id("de-DELFI_de:14612:28:8:Gleis19", KNOWN) is None
    assert parse_stop_id("jdf_", KNOWN) is None


def test_the_shipped_datasets_load_longest_tag_first() -> None:
    tags = [d.tag for d in load_datasets()]
    assert {"cz-CZPTT", "cz-JDF-merged", "cz-PID", "czptt", "jdf"} <= set(tags)
    assert [len(t) for t in tags] == sorted((len(t) for t in tags), reverse=True)


def test_a_dataset_resolved_by_key_needs_its_namespace(tmp_path: Path) -> None:
    path = tmp_path / "datasets.toml"
    path.write_text('version = "1"\n[[dataset]]\ntag = "x"\nresolve = "source_key"\n')
    with pytest.raises(ValueError, match="without namespace"):
        load_datasets(path)


def _call(sequence: int, location: str) -> TripCall:
    return TripCall(
        sequence=sequence,
        location_id=location,
        name=location,
        latitude=None,
        longitude=None,
        boarding="regular",
        alighting="regular",
        flags=[],
        platform=None,
        scheduled_arrival=None,
        scheduled_departure=None,
        estimated_arrival=None,
        estimated_departure=None,
        status="scheduled",
        source_class=None,
    )


def test_a_leg_spans_boarding_to_alighting_on_a_loop() -> None:
    calls = [_call(1, "a"), _call(2, "b"), _call(3, "c"), _call(4, "a"), _call(5, "d")]
    assert [c.sequence for c in leg_span(calls, "b", "c") or []] == [2, 3]
    # `a` twice: the first `a` that `d` follows.
    assert [c.sequence for c in leg_span(calls, "a", "d") or []] == [1, 2, 3, 4, 5]
    assert leg_span(calls, "c", "b") is None
    assert leg_span(calls, None, "b") is None
