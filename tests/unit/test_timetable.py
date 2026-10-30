"""A printed timetable's layout: directions and the one stop order each reads down."""

from __future__ import annotations

from obehy.api.timetable import directions, merge, rows_of


def _stops(rows: list[tuple[str, int]]) -> list[str]:
    return [stop if n == 1 else f"{stop}#{n}" for stop, n in rows]


def test_a_line_with_short_turns_keeps_every_stop_in_order() -> None:
    whole = rows_of("ABCDE")
    assert _stops(merge([whole, whole, rows_of("ABC"), rows_of("CDE")])) == list("ABCDE")


def test_a_branch_lists_its_stops_after_where_it_leaves() -> None:
    order = merge([rows_of("ABCD"), rows_of("ABCD"), rows_of("ABXY")])
    assert _stops(order) == ["A", "B", "X", "Y", "C", "D"]


def test_a_stop_the_backbone_skips_goes_between_its_neighbours() -> None:
    assert _stops(merge([rows_of("ABD"), rows_of("ABD"), rows_of("ABCD")])) == list("ABCD")


def test_a_loop_visits_its_terminus_twice() -> None:
    assert rows_of("ABCA") == [("A", 1), ("B", 1), ("C", 1), ("A", 2)]
    assert _stops(merge([rows_of("ABCA")])) == ["A", "B", "C", "A#2"]


def test_directions_follow_the_feed_else_the_way_trips_run() -> None:
    there, back = rows_of("ABC"), rows_of("CBA")
    assert directions([(1, back), (0, there), (0, there)]) == [[1, 2], [0]]
    assert directions([(None, there), (None, back), (None, there)]) == [[0, 2], [1]]
