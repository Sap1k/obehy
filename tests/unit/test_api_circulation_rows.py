"""A source's oběhy as rows (`circulations.rows_of`): a vehicle's chains in one row, in the
order they start, rows by name with numbers as numbers."""

from __future__ import annotations

from datetime import date, datetime

from obehy.api import schema
from obehy.api.circulations import rows_of

DAY = date(2026, 10, 6)


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


def test_a_vehicles_chains_are_one_row_in_the_order_they_start() -> None:
    morning = chain("1001", journey("a", "06:00:00", "06:15:00"))
    evening = chain("1001", journey("b", "15:00:00", "15:40:00"))
    (row,) = rows_of([evening, morning])
    assert [c.journeys[0].trip_id for c in row.chains] == ["a", "b"]


def test_rows_go_by_name_numbers_as_numbers() -> None:
    rows = rows_of(
        [
            chain("1001", journey("a", "06:00:00", "06:10:00")),
            chain("999", journey("b", "07:00:00", "07:10:00")),
        ]
    )
    assert [r.display_name for r in rows] == ["999", "1001"]
