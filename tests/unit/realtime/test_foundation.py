from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from obehy.realtime.index import IndexMiss
from obehy.realtime.policy import PolicyError, load_policy, parse_policy
from tests.realtime.builder import timetable, with_unknown


def test_policy_loads_and_resolves_modes() -> None:
    policy = load_policy()
    assert policy.version == "1"
    assert policy.time.max_delay_s("rail") == 14400
    assert policy.time.max_delay_s("bus") == 7200
    assert policy.lifecycle.off_route_base_m("tram") == 150


def test_policy_without_a_value_does_not_load() -> None:
    import tomllib

    from obehy.realtime.policy import POLICY

    document: dict[str, Any] = tomllib.loads(POLICY.read_text(encoding="utf-8"))
    del document["lifecycle"]["off_route_k"]
    with pytest.raises(PolicyError, match=r"lifecycle\.off_route_k"):
        parse_policy(document)


def test_builder_index_has_keys_trips_shapes_and_visits() -> None:
    day = date(2026, 10, 8)
    index = (
        timetable()
        .trip("582492:143", days=[day], calls=[("A", "23:50"), ("B", "24:05"), ("A", "24:20")])
        .index()
    )
    (entry,) = index.keys("cis:line_trip", "582492:143")
    trip = index.trip(entry.public_id)
    assert [c.visit_n for c in trip.calls] == [1, 1, 2]
    assert (trip.start, trip.end) == (23 * 3600 + 50 * 60, 24 * 3600 + 20 * 60)
    assert trip.calls[1].distance_m is not None
    assert abs(trip.calls[1].distance_m - 1000) < 10
    assert index.runs_on(trip.service_id, day)
    assert not index.runs_on(trip.service_id, date(2026, 10, 9))
    assert index.keys("cis:line", "582492")


def test_unloaded_data_is_an_error_and_unknown_keys_are_empty() -> None:
    index = with_unknown(timetable().index(), ("cis:line_trip", "100001:1"))
    assert index.keys("cis:line_trip", "100001:1") == ()
    assert index.keys("cis:line", "100001") == ()
    with pytest.raises(IndexMiss):
        index.keys("cis:line_trip", "100001:2")


def test_facts_round_trip_through_json() -> None:
    import json
    from datetime import UTC, datetime

    from obehy.realtime.model import (
        Delay,
        Fact,
        NextStop,
        Position,
        SourceState,
        StopEvent,
        TripKey,
        VehicleKey,
    )
    from obehy.realtime.model_json import facts_from_json, facts_to_json
    from obehy.realtime.times import instant

    facts: tuple[Fact, ...] = (
        VehicleKey("405012"),
        TripKey("cis:line_trip", "582492:143"),
        Position(50.66, 14.03, None),
        Delay(-60, "unknown"),
        SourceState("1"),
        StopEvent("A", "departure", instant(datetime(2026, 10, 8, 10, tzinfo=UTC))),
        NextStop("B"),
    )
    stored = json.loads(json.dumps(facts_to_json(facts)))
    assert facts_from_json(stored) == facts
