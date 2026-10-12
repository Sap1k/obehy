"""Where a trip's stops lie on its shape (timeline/path.py)."""

from __future__ import annotations

from datetime import date

from obehy.realtime.geo import cumulative_m
from obehy.realtime.index import Shape
from obehy.realtime.timeline.path import build_path
from tests.realtime.builder import timetable

LON0, LAT0 = 14.0, 50.0
LON_M, LAT_M = 1.399e-5, 8.993e-6  # degrees per metre at 50° N


def _at(x: float, y: float) -> tuple[float, float]:
    return (LON0 + x * LON_M, LAT0 + y * LAT_M)


def test_a_zavlek_stop_nearer_the_return_lane_keeps_its_outbound_visit() -> None:
    # 552650:1135 at Brňany,záv. (2026-10-12): a side road driven out and back, two lanes 5 m
    # apart, the stop pin a metre nearer the return lane. Each visit takes its own leg.
    tt = timetable()
    for name, (x, y) in {
        "A": (0, 0),
        "J": (1003, -200),
        "B": (1002.5, -1000),
        "E": (1002.5, -2000),
        "D": (3000, 0),
    }.items():
        tt.stop(name, *_at(x, y))
    calls = [("A", "08:00"), ("J", "08:02"), ("B", "08:03"), ("E", "08:05")]
    calls += [("B", "08:07"), ("J", "08:08"), ("D", "08:10")]
    tt.trip("552650:1135", days=[date(2026, 10, 12)], calls=calls)
    index = tt.index()
    (trip,) = index.trips.values()
    assert trip.shape_id is not None
    points = tuple(
        _at(x, y)
        for x, y in [(0, 0), (1000, 0), (1000, -2000), (1005, -2000), (1005, 0), (3000, 0)]
    )
    index.shapes[trip.shape_id] = Shape(trip.shape_id, points, cumulative_m(points))

    placed = build_path(trip, index).call_distances_m
    expected = [0, 1200, 2000, 3002.5, 4005, 4805, 7000]
    assert [round(d) for d in placed] == [round(e) for e in expected]
