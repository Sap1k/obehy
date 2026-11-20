"""Delay history over a segment of a journey (`api/segments.py`)."""

from __future__ import annotations

from datetime import date

from obehy.api import schema
from obehy.api.limits import load_limits
from obehy.api.segments import call_delay, run, summary

LIMITS = load_limits()


def call(location: str, arrival: int | None, departure: int | None) -> schema.HistoryCall:
    return schema.HistoryCall(
        location_id=location,
        name=location,
        scheduled_arrival=None,
        scheduled_departure=None,
        actual_arrival=None,
        actual_departure=None,
        arrival_delay_s=arrival,
        departure_delay_s=departure,
        arrival_estimated=False,
        departure_estimated=False,
    )


def day(on: str, calls: list[schema.HistoryCall], cancelled: bool = False) -> schema.HistoryDay:
    return schema.HistoryDay(
        service_date=date.fromisoformat(on),
        trip_id="t",
        cancelled=cancelled,
        vehicles=[],
        worked_by=[],
        calls=calls,
    )


def delay(seconds: int | None, estimated: bool = False) -> schema.HistoryDelay:
    return schema.HistoryDelay(delay_s=seconds, estimated=estimated)


def test_a_run_is_measured_from_where_the_rider_boards_to_where_they_get_off() -> None:
    # Early at the start (it does not matter to one boarding at B), late after.
    d = day("2026-10-12", [call("A", None, -180), call("B", 0, 60), call("C", 300, 360)])
    segment = run(d, "B", "C")
    assert segment is not None
    assert (segment.first_call, segment.last_call) == (1, 2)
    assert (segment.start, segment.end, segment.worst) == (delay(60), delay(300), delay(300))
    assert segment.delays == [delay(60), delay(300)]


def test_the_end_takes_the_arrival_else_the_departure() -> None:
    assert call_delay(call("C", None, 240), end=True) == delay(240)
    assert call_delay(call("C", 120, 240), end=True) == delay(120)
    assert call_delay(call("C", 120, 240), end=False) == delay(240)


def test_a_run_lost_before_the_end_counts_by_its_last_measured_delay_estimated() -> None:
    lost = day("2026-10-12", [call("A", None, 0), call("B", 600, 660), call("C", 1800, 1800),
                              call("D", None, None)])  # fmt: skip
    segment = run(lost, "A", "D")
    assert segment is not None and segment.end == delay(1800, estimated=True)


def test_a_run_first_seen_after_the_start_counts_by_its_first_measured_delay() -> None:
    late = day("2026-10-12", [call("A", None, None), call("B", 120, 120), call("C", 120, None)])
    segment = run(late, "A", "C")
    assert segment is not None and segment.start == delay(120, estimated=True)


def test_a_day_not_calling_at_both_has_no_run() -> None:
    assert run(day("2026-10-12", [call("A", None, 0), call("B", 0, None)]), "A", "C") is None
    assert run(day("2026-10-12", [call("B", 0, 0), call("A", 0, None)]), "A", "B") is None


def test_the_summary_keeps_the_bad_day_and_uses_the_apis_on_time_band() -> None:
    days = [
        day("2026-10-12", [call("A", None, 0), call("B", 1800, 1800), call("C", None, None)]),
        day("2026-10-11", [call("A", None, 0), call("B", 0, 0), call("C", 170, None)]),
        day("2026-10-10", [], cancelled=True),
    ]
    days = [d.model_copy(update={"segment": run(d, "A", "C")}) for d in days]
    result = summary(days, "A", "C", LIMITS)
    assert result.runs == 2
    assert result.on_time_share == 0.5  # 170 s is within the band (up to 179 s late)
    assert result.median_delay_s == 170 and result.p90_delay_s == 1800
