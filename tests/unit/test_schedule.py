from datetime import date

import pytest

from obehy.domain.identifiers import CanonicalId, CisLineId, CisTripId, TrainNumber
from obehy.domain.schedule import (
    AmbiguousTripError,
    Direction,
    RailTripCandidate,
    RoadTripCandidate,
    ServiceCalendar,
    ServiceTime,
    TripNotFoundError,
    resolve_road_trip,
    resolve_train_segment,
)


def calendar(*, added: frozenset[date] = frozenset()) -> ServiceCalendar:
    return ServiceCalendar(date(2026, 7, 1), date(2026, 7, 31), frozenset(range(7)), added)


def test_service_calendar_exceptions_and_overnight_time() -> None:
    removed = date(2026, 7, 18)
    item = ServiceCalendar(
        date(2026, 7, 1),
        date(2026, 7, 31),
        frozenset({0}),
        added_dates=frozenset({date(2026, 7, 19)}),
        removed_dates=frozenset({removed}),
    )
    assert item.operates_on(date(2026, 7, 19))
    assert not item.operates_on(removed)
    assert ServiceTime(25 * 3600 + 5).seconds == 90005


def test_road_resolution_requires_exactly_one_active_variant() -> None:
    line = CisLineId("001588")
    trip = CisTripId(7)
    service_date = date(2026, 7, 18)
    first = RoadTripCandidate(CanonicalId("T000000001"), line, trip, calendar())
    assert (
        resolve_road_trip([first], line, trip, service_date).canonical_trip_id
        == first.canonical_trip_id
    )
    with pytest.raises(AmbiguousTripError):
        resolve_road_trip(
            [first, RoadTripCandidate(CanonicalId("T000000002"), line, trip, calendar())],
            line,
            trip,
            service_date,
        )


def test_train_segment_is_an_exact_ordered_subsequence() -> None:
    lhotka = CanonicalId("S000000001")
    smrkov = CanonicalId("S000000002")
    jedlova = CanonicalId("S000000003")
    candidate = RailTripCandidate(
        CanonicalId("T000000001"),
        TrainNumber(9001),
        Direction.OUTBOUND,
        calendar(),
        (lhotka, smrkov, jedlova),
        (10, 30, 40),
    )
    match = resolve_train_segment(
        [candidate],
        TrainNumber(9001),
        date(2026, 7, 18),
        Direction.OUTBOUND,
        (smrkov, jedlova),
    )
    assert match.matched_call_sequences == (30, 40)
    with pytest.raises(TripNotFoundError):
        resolve_train_segment(
            [candidate],
            TrainNumber(9001),
            date(2026, 7, 18),
            Direction.OUTBOUND,
            (jedlova, smrkov),
        )
    with pytest.raises(TripNotFoundError):
        resolve_train_segment(
            [candidate],
            TrainNumber(9001),
            date(2026, 7, 18),
            Direction.INBOUND,
            (smrkov, jedlova),
        )
    with pytest.raises(TripNotFoundError):
        resolve_train_segment(
            [candidate],
            TrainNumber(9001),
            date(2026, 8, 1),
            Direction.OUTBOUND,
            (smrkov, jedlova),
        )
    with pytest.raises(TripNotFoundError):
        resolve_train_segment(
            [candidate],
            TrainNumber(9001),
            date(2026, 7, 18),
            Direction.OUTBOUND,
            (smrkov, CanonicalId("S000000004")),
        )
