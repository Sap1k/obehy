"""Per-call public model: estimate, status and source class (docs/R1_SLICE.md section 1).

- A call with an observed event is `actual`; its estimate is the middle of the event interval.
- A call behind progress without an event (passed while unobserved, or bound mid-trip) is
  `no_realtime`.
- A call ahead of progress is `predicted` from the current delay when one is known, else
  `scheduled`. Predictions never precede an earlier call's estimate or the last fix (monotone
  repair).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from obehy.realtime.index import Trip
from obehy.realtime.model import CallState, Instance, Interval, SourceClass
from obehy.realtime.times import Instant, ServiceTime


def _middle(interval: Interval) -> Instant:
    return Instant(interval.lo + (interval.hi - interval.lo) / 2)


def estimate(instance: Instance, trip: Trip, delay_class: SourceClass | None) -> Instance:
    day = instance.journey.service_date
    delay = None if instance.delay_s is None else timedelta(seconds=instance.delay_s)
    reached = instance.progress.call_index if instance.progress is not None else -1
    floor: Instant | None = instance.progress.at if instance.progress is not None else None
    calls: list[CallState] = []
    for i, (state, call) in enumerate(zip(instance.calls, trip.calls, strict=True)):
        arrival = state.arrival
        departure = state.departure
        if arrival is not None or departure is not None:
            est_arr = _middle(arrival) if arrival is not None else None
            est_dep = _middle(departure) if departure is not None else None
            calls.append(
                replace(
                    state,
                    estimated_arrival=est_arr,
                    estimated_departure=est_dep,
                    status="actual",
                    source_class="gps",
                )
            )
            last = est_dep or est_arr
            floor = last if floor is None or (last is not None and last > floor) else floor
            continue
        if i <= reached or instance.lifecycle == "finished":
            calls.append(
                replace(
                    state,
                    estimated_arrival=None,
                    estimated_departure=None,
                    status="no_realtime",
                    source_class=None,
                )
            )
            continue
        if delay is None:
            calls.append(
                replace(
                    state,
                    estimated_arrival=None,
                    estimated_departure=None,
                    status="scheduled",
                    source_class=None,
                )
            )
            continue
        est_arr = _shifted(day, call.arrival, delay, floor)
        if est_arr is not None:
            floor = est_arr
        est_dep = _shifted(day, call.departure, delay, floor)
        if est_dep is not None:
            floor = est_dep
        calls.append(
            replace(
                state,
                estimated_arrival=est_arr,
                estimated_departure=est_dep,
                status="predicted",
                source_class=delay_class,
            )
        )
    return replace(instance, calls=tuple(calls))


def _shifted(
    day: date, seconds: int | None, delay: timedelta, floor: Instant | None
) -> Instant | None:
    if seconds is None:
        return None
    value = Instant(ServiceTime(day, seconds).instant() + delay)
    return floor if floor is not None and value < floor else value
