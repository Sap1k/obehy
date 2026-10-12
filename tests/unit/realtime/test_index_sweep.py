"""The worker's hourly index sweep (BASE_PLAN.md section 18.3): what a kept key needs stays."""

from __future__ import annotations

from datetime import date

from obehy.realtime.index import Index
from obehy.realtime.infer.keyed import bind
from obehy.realtime.model import TripKey
from obehy.realtime.policy import load_policy
from tests.realtime.builder import timetable
from tests.realtime.observations import local

POLICY = load_policy()
D7, D8 = date(2026, 10, 7), date(2026, 10, 8)


def _index() -> Index:
    return (
        timetable()
        .trip("582492:143", days=[D8], calls=[("A", "08:00"), ("B", "08:05")])
        .trip("582492:145", days=[D8], calls=[("C", "09:00"), ("D", "09:05")])
        .trip("600001:1", days=[D8], calls=[("E", "10:00"), ("F", "10:05")], run=("PA1", 1))
        .trip("600001:2", days=[D8], calls=[("F", "10:05"), ("G", "10:10")], run=("PA1", 2))
        .index()
    )


def test_a_sweep_keeps_what_kept_keys_and_live_trips_need() -> None:
    index = _index()
    key = TripKey("cis:line_trip", "582492:143")
    before = bind(key, local("2026-10-08 08:01"), index, POLICY)
    index.sweep({(key.namespace, key.key)}, (), {"jdf:t3"}, D7)  # the runner: from yesterday

    # The kept key with its line, the live trip with every part of its run.
    assert set(index.key_entries) == {("cis:line_trip", "582492:143"), ("cis:line", "582492")}
    assert set(index.trips) == {"jdf:t1", "jdf:t3", "jdf:t4"}
    assert set(index.runs) == {"PA1"}
    assert set(index.shapes) == {"jdf:t1:shape", "jdf:t3:shape", "jdf:t4:shape"}
    assert {loc.split(":")[1] for loc in index.locations} == {"A", "B", "E", "F", "G"}
    assert set(index.service_dates) == {"jdf:t1:service", "jdf:t3:service", "jdf:t4:service"}
    assert min(index.loaded_dates) == D7
    # What was kept binds as before; what was dropped is no longer loaded, so the loader
    # loads it again when an observation names it.
    assert bind(key, local("2026-10-08 08:01"), index, POLICY) == before
    assert index.missing({("cis:line_trip", "582492:145")}) == {("cis:line_trip", "582492:145")}


def test_a_kept_line_keeps_its_trips_keys() -> None:
    index = _index()
    index.sweep((), {("cis:line", "582492")}, (), D8)
    assert set(index.trips) == {"jdf:t1", "jdf:t2"}
    assert index.lines == {("cis:line", "582492"): ("582492:143", "582492:145")}
