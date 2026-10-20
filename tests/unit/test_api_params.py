"""The API's parameter rules: viewports, search words, the departure window, vehicle IDs."""

from __future__ import annotations

from datetime import date

import pytest

from obehy.api.params import (
    Cursor,
    ParameterError,
    date_span,
    decode_cursor,
    encode_cursor,
    parse_bbox,
    parse_near,
    schedule_spans,
    search_words,
    split_vehicle_id,
)
from obehy.api.trips import by_key_path, trip_path
from obehy.realtime.times import ServiceTime
from tests.realtime.observations import local


def test_bbox_is_west_south_east_north_within_the_area_cap() -> None:
    box = parse_bbox("14.4,50.0,14.6,50.1", max_area=0.5)
    assert (box.west, box.south, box.east, box.north) == (14.4, 50.0, 14.6, 50.1)
    for text in ("14.6,50.0,14.4,50.1", "1,2,3", "a,b,c,d", "14,50,nan,51", "0,0,200,1"):
        with pytest.raises(ParameterError):
            parse_bbox(text, max_area=0.5)
    with pytest.raises(ParameterError, match="zoom in"):
        parse_bbox("12,48,19,51", max_area=0.5)


def test_search_words_are_letters_and_digits_never_wildcards() -> None:
    assert search_words("Česká Lípa,,aut.nádr.") == ["Česká", "Lípa", "aut", "nádr"]
    assert search_words("100%_x") == ["100", "x"]
    assert search_words(" ,. ") == []


def _seconds(day: date, at: str) -> int:
    return int((local(at) - ServiceTime(day, 0).instant()).total_seconds())


def test_the_window_covers_the_day_before_for_times_past_midnight() -> None:
    spans = schedule_spans(local("2026-10-06 00:10"), local("2026-10-06 01:10"))
    assert spans.dates == [date(2026, 10, 5), date(2026, 10, 6)]
    # 24:30 on the 5th lies inside; the slack is an hour either side.
    yesterday_lo, yesterday_hi = spans.lo[0], spans.hi[0]
    assert yesterday_lo <= 24 * 3600 + 1800 <= yesterday_hi
    assert yesterday_lo == _seconds(date(2026, 10, 5), "2026-10-06 00:10") - 3600
    assert spans.lo[1] == 0 and spans.hi[1] == 3600 + 600 + 3600


def test_the_window_ends_with_the_day_it_ends_on() -> None:
    spans = schedule_spans(local("2026-10-06 23:00"), local("2026-10-07 00:30"))
    assert spans.dates == [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    spans = schedule_spans(local("2026-10-06 22:00"), local("2026-10-06 23:30"))
    assert spans.dates == [date(2026, 10, 5), date(2026, 10, 6)]


def test_the_window_holds_across_the_autumn_change() -> None:
    # 2026-10-25: 03:00 CEST becomes 02:00 CET. 02:30 wall-clock repeats; seconds stay wall time.
    spans = schedule_spans(local("2026-10-25 01:30"), local("2026-10-25 03:30"))
    day = spans.dates.index(date(2026, 10, 25))
    assert spans.lo[day] <= 2 * 3600 + 1800 <= spans.hi[day]
    assert spans.lo[day] <= 3 * 3600 + 1800 <= spans.hi[day]


def test_a_vehicle_id_is_source_and_source_vehicle_id() -> None:
    assert split_vehicle_id("pid:service-3-1234") == ("pid", "service-3-1234")
    assert split_vehicle_id("duk:a:b") == ("duk", "a:b")
    for bad in ("pid", ":1", "pid:"):
        with pytest.raises(ParameterError):
            split_vehicle_id(bad)


def test_trip_paths_escape_what_trip_ids_contain() -> None:
    # CZPTT trip IDs carry a percent-encoded PA ID; the path must encode the percent again.
    assert (
        trip_path("czptt:trip:Pa%3A0054%3AKT----12732A%3A00%3A2026:1", date(2026, 10, 15))
        == "/api/v1/trips/czptt:trip:Pa%253A0054%253AKT----12732A%253A00%253A2026:1/2026-10-15"
    )
    assert by_key_path("cis:line_trip", "100133:1027", date(2026, 10, 15)) == (
        "/api/v1/trips/by-key/cis:line_trip/100133:1027/2026-10-15"
    )


def test_a_cursor_round_trips_and_rejects_anything_else() -> None:
    cursor = Cursor(
        local("2026-10-06 06:10"),
        local("2026-10-06 06:00"),
        "czptt:trip:Pa%3A1:1",
        date(2026, 10, 5),
        "jdf:stop:1",
    )
    assert decode_cursor(encode_cursor(cursor)) == cursor
    for bad in ("", "bm90IGEgY3Vyc29y", "e30"):
        with pytest.raises(ParameterError):
            decode_cursor(bad)


def test_near_and_date_spans() -> None:
    assert parse_near("50.08,14.43") == (50.08, 14.43)
    for bad in ("14", "a,b", "95,14"):
        with pytest.raises(ParameterError):
            parse_near(bad)
    today = date(2026, 10, 20)
    assert date_span(None, None, today, 7, 31) == (date(2026, 10, 14), today)
    assert date_span(date(2026, 10, 1), date(2026, 10, 3), today, 7, 31)[0] == date(2026, 10, 1)
    with pytest.raises(ParameterError, match="31 days"):
        date_span(date(2026, 1, 1), None, today, 7, 31)
    with pytest.raises(ParameterError, match="after"):
        date_span(date(2026, 10, 5), date(2026, 10, 4), today, 7, 31)
