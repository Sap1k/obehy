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
from datetime import timedelta

from obehy.realtime.model import CallState, CallStatus, Instance, Interval, SourceClass
from obehy.realtime.timeline.path import Scheduled
from obehy.realtime.times import Instant


def _middle(interval: Interval) -> Instant:
    return Instant(interval.lo + (interval.hi - interval.lo) / 2)


def _call(
    state: CallState,
    arrival: Instant | None,
    departure: Instant | None,
    status: CallStatus,
    source_class: SourceClass | None,
) -> CallState:
    return CallState(
        state.sequence,
        state.location_id,
        state.visit_n,
        state.arrival,
        state.departure,
        arrival,
        departure,
        status,
        source_class,
    )


def estimate(instance: Instance, scheduled: Scheduled) -> Instance:
    """Per-call estimates; computed when state is emitted, not on every fix."""

    delay_class: SourceClass | None = "source" if instance.delay_s is not None else None
    delay = None if instance.delay_s is None else timedelta(seconds=instance.delay_s)
    reached = instance.progress.call_index if instance.progress is not None else -1
    # A call with an observed event means every earlier call was passed, observed or not.
    for i, state in enumerate(instance.calls):
        if (state.arrival is not None or state.departure is not None) and i > reached:
            reached = i - 1 if state.departure is None else i
    floor: Instant | None = instance.progress.at if instance.progress is not None else None
    finished = instance.lifecycle == "finished"
    calls: list[CallState] = []
    for i, (state, (sched_arr, sched_dep)) in enumerate(
        zip(instance.calls, scheduled, strict=True)
    ):
        arrival = state.arrival
        departure = state.departure
        if arrival is not None or departure is not None:
            est_arr = _middle(arrival) if arrival is not None else None
            est_dep = _middle(departure) if departure is not None else None
            calls.append(_call(state, est_arr, est_dep, "actual", "gps"))
            last = est_dep or est_arr
            floor = last if floor is None or (last is not None and last > floor) else floor
        elif i <= reached or finished:
            calls.append(_call(state, None, None, "no_realtime", None))
        elif delay is None:
            calls.append(_call(state, None, None, "scheduled", None))
        else:
            est_arr = _shifted(sched_arr, delay, floor)
            if est_arr is not None:
                floor = est_arr
            est_dep = _shifted(sched_dep, delay, floor)
            if est_dep is not None:
                floor = est_dep
            calls.append(_call(state, est_arr, est_dep, "predicted", delay_class))
    return replace(instance, calls=tuple(calls))


def _shifted(scheduled: Instant | None, delay: timedelta, floor: Instant | None) -> Instant | None:
    if scheduled is None:
        return None
    value = Instant(scheduled + delay)
    return floor if floor is not None and value < floor else value
