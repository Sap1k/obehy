"""The one delay rule every answer uses (api/punctuality.py)."""

from __future__ import annotations

from datetime import datetime

from obehy.api.punctuality import minutes, of_seconds, of_time, rider_time


def at(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-12T{text}+02:00")


def test_a_delay_is_the_shown_minutes_cut_not_rounded() -> None:
    # 08:00 scheduled, expected 08:02:59: the board shows 08:02, two minutes late.
    assert minutes(at("08:00:00"), at("08:02:59")) == 2
    assert minutes(at("08:00:00"), at("08:03:00")) == 3
    assert minutes(at("08:00:30"), at("07:59:59")) == -1


def test_the_bands_are_the_boards() -> None:
    def band(late: str) -> str:
        return of_time(at("08:00:00"), at(late), "predicted").punctuality

    times = ("07:58:00", "07:59:00", "08:02:59", "08:03:00", "08:09:59", "08:10:00")
    bands = ["early", "on_time", "on_time", "late", "late", "very_late"]
    assert [band(t) for t in times] == bands


def test_no_live_estimate_has_no_band_and_stale_keeps_only_its_minutes() -> None:
    assert of_time(at("08:00:00"), None, "predicted").punctuality == "unknown"
    assert of_time(at("08:00:00"), at("08:05:00"), "scheduled").delay_min is None
    stale = of_time(at("08:00:00"), at("08:05:00"), "predicted", stale=True)
    assert (stale.delay_min, stale.punctuality) == (5, "unknown")
    assert of_time(at("08:00:00"), at("08:05:00"), "cancelled").punctuality == "cancelled"
    assert of_seconds(-61).delay_min == -2 and of_seconds(None).punctuality == "unknown"


def test_a_rider_meets_the_departure_and_at_the_end_the_arrival() -> None:
    arrival, departure = (at("08:00:00"), at("08:01:00")), (at("08:01:00"), at("08:03:00"))
    assert rider_time(arrival, departure, last=False) == departure
    assert rider_time(arrival, departure, last=True) == arrival
    # No scheduled departure (the last call of a run part): its arrival.
    assert rider_time(arrival, (None, None), last=False) == arrival
    # The estimate missing on the rider's side: the other side's.
    assert rider_time(arrival, (at("08:01:00"), None), last=False) == (
        at("08:01:00"),
        at("08:01:00"),  # the arrival's estimate
    )
