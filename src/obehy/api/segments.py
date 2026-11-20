"""How late a journey was over a segment of its calls, per day and over the days.

The rider boards at one stop and gets off at another; outside that segment an early or late
run does not matter to them. Every client asks the API for the same thing (the web's trip
history, the native app), so it is decided here once:

- a call's delay as the rider meets it: its departure, at the segment's end its arrival (the
  other one when that is missing);
- a run lost just before the segment's end, or first seen just after its start, still counts:
  by the nearest delay measured inside the segment, flagged estimated (dropping it would leave
  out exactly the bad days);
- the summary over the days: runs measured at the end, the share on time there by the API's
  on-time band (`limits.on_time_*`), the median and the 90th percentile.
"""

from __future__ import annotations

import math

from obehy.api import schema
from obehy.api.limits import Limits

NONE = schema.HistoryDelay(delay_s=None, estimated=False)


def call_delay(call: schema.HistoryCall, end: bool) -> schema.HistoryDelay:
    """A call's delay as the rider meets it: its departure, at the segment's end its arrival;
    the other one when that is missing."""

    arrival = schema.HistoryDelay(delay_s=call.arrival_delay_s, estimated=call.arrival_estimated)
    departure = schema.HistoryDelay(
        delay_s=call.departure_delay_s, estimated=call.departure_estimated
    )
    first, second = (arrival, departure) if end else (departure, arrival)
    if first.delay_s is not None:
        return first
    return second if second.delay_s is not None else NONE


def _nearest(
    delay: schema.HistoryDelay, fallback: schema.HistoryDelay | None
) -> schema.HistoryDelay:
    if delay.delay_s is not None or fallback is None:
        return delay
    return schema.HistoryDelay(delay_s=fallback.delay_s, estimated=True)


def run(day: schema.HistoryDay, start: str, end: str) -> schema.HistorySegmentRun | None:
    """The day's run from the stop `start` to the stop `end` (its first visit after the
    start); None if the day does not call at both in that order."""

    first = next((i for i, c in enumerate(day.calls) if c.location_id == start), None)
    if first is None:
        return None
    last = next(
        (i for i in range(first + 1, len(day.calls)) if day.calls[i].location_id == end), None
    )
    if last is None:
        return None
    calls = day.calls[first : last + 1]
    delays = [call_delay(c, i == len(calls) - 1) for i, c in enumerate(calls)]
    measured = [d for d in delays if d.delay_s is not None]
    worst = max(measured, key=lambda d: d.delay_s or 0, default=NONE)
    return schema.HistorySegmentRun(
        first_call=first,
        last_call=last,
        start=_nearest(delays[0], measured[0] if measured else None),
        end=_nearest(delays[-1], measured[-1] if measured else None),
        worst=worst,
        delays=delays,
    )


def summary(
    days: list[schema.HistoryDay], start: str, end: str, limits: Limits
) -> schema.HistorySegment:
    """How the segment usually ends over the days not cancelled."""

    ends = sorted(
        d.segment.end.delay_s
        for d in days
        if not d.cancelled and d.segment is not None and d.segment.end.delay_s is not None
    )
    on_time = [x for x in ends if limits.on_time_early_s <= x <= limits.on_time_late_s]
    return schema.HistorySegment(
        start_location_id=start,
        end_location_id=end,
        runs=len(ends),
        on_time_share=len(on_time) / len(ends) if ends else None,
        median_delay_s=percentile(ends, 0.5) if ends else None,
        p90_delay_s=percentile(ends, 0.9) if ends else None,
    )


def percentile(ordered: list[int], share: float) -> int:
    """The nearest-rank percentile of sorted values: the least value at least `share` of them
    are no later than."""

    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]
