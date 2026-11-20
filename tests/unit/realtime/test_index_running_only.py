"""The index holds only trips that run on its loaded dates (`index_sql.IndexLoader`).

A key names every variant of a trip in the timetable year; loading them all kept the worker's
memory at several times what its live journeys need. The loader records each named trip's
service, loads the variants that run on a loaded date, and loads the others when a date they
run on is loaded, including after a sweep. Read from release files, as replay does.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from obehy.realtime.index_sql import IndexLoader, file_loads
from obehy.realtime.infer.keyed import running
from tests.db.serving_fixture import write_release
from tests.realtime.builder import serving_rows, timetable

DAY = date(2026, 10, 8)
NEXT = DAY + timedelta(days=1)
LATER = DAY + timedelta(days=5)
KEY = ("cis:line_trip", "582492:144")


def _loader(tmp_path: Path) -> IndexLoader:
    calls = [("B", "06:00"), ("C", "06:20")]
    tt = (
        timetable()
        .trip(KEY[1], days=[DAY], calls=calls, trip_id="jdf:t-thu")
        .trip(KEY[1], days=[NEXT], calls=calls, trip_id="jdf:t-fri")
        .trip(KEY[1], days=[LATER], calls=calls, trip_id="jdf:t-later")
    )
    release = write_release(tmp_path, "run-running-only", jdf=serving_rows(tt))
    return IndexLoader(None, file_loads(release), "jdf")


def test_only_the_variants_running_on_loaded_dates_load(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    index = loader.ensure({KEY}, {DAY})
    assert set(index.trips) == {"jdf:t-thu"}
    assert set(index.trip_services) == {"jdf:t-thu", "jdf:t-fri", "jdf:t-later"}
    assert [t.trip_id for _, t in running(index, *KEY, [DAY])] == ["jdf:t-thu"]


def test_a_new_date_loads_the_variants_running_on_it(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader.ensure({KEY}, {DAY})
    index = loader.ensure(set(), {NEXT})
    assert set(index.trips) == {"jdf:t-thu", "jdf:t-fri"}
    assert [t.trip_id for _, t in running(index, *KEY, [NEXT])] == ["jdf:t-fri"]


def test_a_sweep_keeps_what_a_later_date_needs(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    index = loader.ensure({KEY}, {DAY})
    index.sweep({KEY}, (), (), DAY)
    assert set(index.trip_services) == {"jdf:t-thu", "jdf:t-fri", "jdf:t-later"}
    index = loader.ensure(set(), {NEXT})
    assert "jdf:t-fri" in index.trips
