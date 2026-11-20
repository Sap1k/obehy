"""A source's oběhy as rows (`circulations.rows_of`): a vehicle's chains in one row, bars in
seconds from the service date's noon less 12 h, rows by name with numbers as numbers."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from obehy.api import schema
from obehy.api.circulations import rows_of
from obehy.realtime.times import service_day_origin

DAY = date(2026, 10, 6)
ORIGIN = service_day_origin(DAY)


def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-06T{clock}+02:00")


def journey(trip: str, start: str, end: str) -> schema.ChainJourney:
    return schema.ChainJourney(
        journey=schema.JourneyKey(namespace="cis:line_trip", key=trip), service_date=DAY,
        trip_id=trip, line=None, route_name=None, headsign=None, origin=None,
        departure=at(start), destination=None, arrival=at(end), first_seen=at(start),
        last_seen=at(end), layover_s=None,
    )  # fmt: skip


def chain(vehicle: str, *journeys: schema.ChainJourney) -> schema.ScopedChain:
    return schema.ScopedChain(
        vehicle_id=f"duk:{vehicle}", source="duk", source_vehicle_id=vehicle, fleet_number=None,
        public_label=None, manufacturer=None, model=None,
        first_seen=journeys[0].first_seen, last_seen=journeys[-1].last_seen,
        journeys=list(journeys), matched=[0],
    )  # fmt: skip


def test_a_vehicles_chains_are_one_row_with_the_break_between_them_kept() -> None:
    morning = chain("1001", journey("a", "06:00:00", "06:15:00"))
    evening = chain("1001", journey("b", "15:00:00", "15:40:00"))
    (row,) = rows_of([evening, morning], ORIGIN)
    assert [b.chain for b in row.bars] == [0, 1]  # in the order they start
    assert [(b.start_s, b.end_s, b.in_scope) for b in row.bars] == [
        (6 * 3600, 6 * 3600 + 15 * 60, True),
        (15 * 3600, 15 * 3600 + 40 * 60, True),
    ]
    assert [(s.start_s, s.end_s) for s in row.breaks] == [(6 * 3600 + 15 * 60, 15 * 3600)]


def test_rows_go_by_name_numbers_as_numbers_and_a_bar_lasts_a_minute() -> None:
    rows = rows_of(
        [
            chain("1001", journey("a", "06:00:00", "06:00:00")),
            chain("999", journey("b", "07:00:00", "07:10:00")),
        ],
        ORIGIN,
    )
    assert [r.display_name for r in rows] == ["999", "1001"]
    assert (rows[1].bars[0].start_s, rows[1].bars[0].end_s) == (6 * 3600, 6 * 3600 + 60)


def test_the_timeline_counts_from_noon_less_twelve_hours() -> None:
    # 25 October 2026 has 25 hours: its noon is still 12:00 on the timeline.
    origin = service_day_origin(date(2026, 10, 25))
    noon = datetime.fromisoformat("2026-10-25T12:00:00+01:00")
    assert noon - origin == timedelta(hours=12)
