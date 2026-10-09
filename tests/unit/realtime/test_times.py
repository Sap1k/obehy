"""Time scenario table T1, T6-T12, T14, T15 (docs/R1_SLICE.md section 2).

T2-T5 are binding scenarios (tests of the core) and T13 is the vehicle-day job.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from obehy.realtime.times import (
    Instant,
    ServiceTime,
    TimeError,
    candidate_service_dates,
    instant,
    read_digits_as_local,
    read_digits_as_utc,
    resolve_clock,
    resolve_local,
    within_skew,
)


def utc(text: str) -> Instant:
    return instant(datetime.fromisoformat(text).replace(tzinfo=UTC))


def st(day: str, clock: str) -> ServiceTime:
    sign = -1 if clock.startswith("-") else 1
    hours, minutes = clock.lstrip("-").split(":")
    return ServiceTime(date.fromisoformat(day), sign * (int(hours) * 3600 + int(minutes) * 60))


def test_t1_times_past_24_continue_into_the_next_day() -> None:
    assert st("2026-10-08", "24:30").instant() == utc("2026-10-08T22:30")


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("01:30", "2026-03-29T00:30"),
        ("02:30", "2026-03-29T01:30"),  # skipped hour: read with the pre-change offset
        ("05:00", "2026-03-29T03:00"),
    ],
)
def test_t6_spring_forward_schedule_times(clock: str, expected: str) -> None:
    assert st("2026-03-29", clock).instant() == utc(expected)


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("02:30", "2026-10-25T00:30"),  # repeated hour: first occurrence
        ("05:00", "2026-10-25T04:00"),
    ],
)
def test_t7_fall_back_schedule_times(clock: str, expected: str) -> None:
    assert st("2026-10-25", clock).instant() == utc(expected)


@pytest.mark.parametrize(
    ("received", "expected"),
    [("2026-10-25T00:31", "2026-10-25T00:30"), ("2026-10-25T01:31", "2026-10-25T01:30")],
)
def test_t8_repeated_hour_takes_the_reading_closest_to_reception(
    received: str, expected: str
) -> None:
    wall = datetime(2026, 10, 25, 2, 30)  # noqa: DTZ001
    assert resolve_local(wall, utc(received)) == utc(expected)


def test_t9_a_local_time_in_the_skipped_hour_is_rejected() -> None:
    wall = datetime(2026, 3, 29, 2, 30)  # noqa: DTZ001
    assert resolve_local(wall, utc("2026-03-29T00:31")) is None


@pytest.mark.parametrize("label", ["+02:00", "+01:00"])
def test_t10_duk_q1_teplice_digits_are_utc_whatever_the_label(label: str) -> None:
    assert read_digits_as_utc(f"2026-07-01T10:00:00{label}") == utc("2026-07-01T10:00")


def test_t11_arriva_q1_digits_are_local_time_labelled_utc() -> None:
    received = utc("2026-07-01T08:00:20")
    assert read_digits_as_local("2026-07-01T10:00:00+00:00", received) == utc("2026-07-01T08:00")


def test_t12_sz_q2_bare_clock_time_across_midnight() -> None:
    received = utc("2026-10-08T22:01")  # 00:01 local on 10-09
    assert resolve_clock(time(23, 59), received) == utc("2026-10-08T21:59")


def test_t14_trip_across_the_fall_back_change_keeps_its_real_length() -> None:
    start = st("2026-10-25", "01:00").instant()
    end = st("2026-10-25", "04:00").instant()
    assert (start, end) == (utc("2026-10-24T23:00"), utc("2026-10-25T03:00"))
    assert end - start == timedelta(hours=4)


def test_t15_a_clock_lagging_the_switch_is_beyond_the_skew_limit() -> None:
    received = utc("2026-10-25T02:00:10")  # 03:00 CET; the bus still reports CEST
    reported = instant(datetime.fromisoformat("2026-10-25T03:00:05+02:00"))
    assert not within_skew(reported, received, timedelta(minutes=20))
    assert within_skew(received - timedelta(seconds=5), received, timedelta(minutes=20))


def test_candidate_service_dates_follow_the_local_calendar() -> None:
    assert candidate_service_dates(utc("2026-10-08T22:20")) == (
        date(2026, 10, 8),
        date(2026, 10, 9),
        date(2026, 10, 10),
    )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(TimeError):
        instant(datetime(2026, 10, 8, 12))  # noqa: DTZ001
