"""Per-call public model: estimate, status and source class (docs/R1_SLICE.md section 1).

- A call with a recorded event is `actual`. One passed inside a reception gap is `inferred`
  (realtime only: history has no event for it). Both take the crossing time interpolated
  between the fixes either side. A call the vehicle has arrived at but not yet left gets a
  predicted departure.
- A call behind progress without a crossing (passed before the journey was first seen) is
  `no_realtime`.
- A call ahead of progress is `predicted` from the current lateness: the tracker's, measured
  from GPS against the timetable, else the source's own delay; with neither it is `scheduled`.
  During a reception gap the last lateness holds; the vehicle is never assumed back on time.

Propagation (BASE_PLAN.md section 20.5) carries the lateness call by call. A late vehicle
recovers time only where the timetable has a real dwell (departure after arrival): it leaves
after the minimum dwell, never before the scheduled departure. Early running is carried
unchanged: whether a vehicle waits for its time is a habit of particular stops, learned from
history later, not a rule. Predictions never precede an earlier call's estimate or the last fix
(monotone repair). There is no uncertainty: each estimate is the best single prediction.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from obehy.realtime.model import CallState, CallStatus, Instance, Interval, SourceClass
from obehy.realtime.policy import PredictionPolicy
from obehy.realtime.timeline.plan import Scheduled
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
    return replace(
        state,
        estimated_arrival=arrival,
        estimated_departure=departure,
        status=status,
        source_class=source_class,
    )


def _current_lateness(instance: Instance) -> tuple[timedelta | None, SourceClass | None]:
    track = instance.track
    if track is not None and track.hypotheses:
        return timedelta(seconds=round(track.hypotheses[0].lateness_s)), "gps"
    if instance.delay_s is not None:
        return timedelta(seconds=instance.delay_s), "source"
    return None, None


def estimate(
    instance: Instance,
    scheduled: Scheduled,
    policy: PredictionPolicy,
    *,
    current: tuple[timedelta | None, SourceClass | None] | None = None,
    anchor: tuple[int, timedelta] | None = None,
) -> Instance:
    """Per-call estimates; computed when state is emitted, not on every fix.

    Rail runs pass the fused current lateness and, from a source's prediction, the lateness at
    one call ahead (`anchor`) from which propagation continues (`timeline.fusion`)."""

    lateness, delay_class = current if current is not None else _current_lateness(instance)
    reached = instance.progress.call_index if instance.progress is not None else -1
    # A call with a crossing means every earlier call was passed, observed or not.
    for i, state in enumerate(instance.calls):
        passed = state.passed_arrival is not None or state.passed_departure is not None
        if passed and i > reached:
            reached = i - 1 if state.passed_departure is None else i
    latest_event = max(
        (
            i
            for i, c in enumerate(instance.calls)
            if c.arrival or c.departure or c.passed_arrival or c.passed_departure
        ),
        default=-1,
    )
    floor: Instant | None = instance.progress.at if instance.progress is not None else None
    finished = instance.lifecycle == "finished"
    last = len(instance.calls) - 1
    calls: list[CallState] = []
    for i, (state, (sched_arr, sched_dep)) in enumerate(
        zip(instance.calls, scheduled, strict=True)
    ):
        recorded = state.arrival is not None or state.departure is not None
        if recorded or state.passed_arrival is not None or state.passed_departure is not None:
            est_arr = _passed(state.passed_arrival, state.arrival)
            est_dep = _passed(state.passed_departure, state.departure)
            status: CallStatus = "actual" if recorded else "inferred"
            # A call with an event after it was left, departure unseen: not a prediction.
            waiting = i >= latest_event
            if est_dep is None and i < last and waiting and not finished and lateness is not None:
                # Arrived, not yet left: the departure is still a prediction, never before the
                # arrival (a source's arrival can be later than the last fix).
                leave_floor = floor if est_arr is None or (floor and floor > est_arr) else est_arr
                est_dep, lateness = _departure(
                    est_arr, sched_arr, sched_dep, lateness, leave_floor, instance.mode, policy
                )
            calls.append(_call(state, est_arr, est_dep, status, "gps"))
            latest = est_dep or est_arr
            if latest is not None and (floor is None or latest > floor):
                floor = latest
        elif i <= reached or finished:
            calls.append(_call(state, None, None, "no_realtime", None))
        elif lateness is None:
            calls.append(_call(state, None, None, "scheduled", None))
        else:
            if anchor is not None and anchor[0] == i:
                lateness = anchor[1]
            est_arr = _shifted(sched_arr, lateness, floor)
            if est_arr is not None:
                floor = est_arr
            est_dep, lateness = _departure(
                est_arr, sched_arr, sched_dep, lateness, floor, instance.mode, policy
            )
            if est_dep is not None:
                floor = est_dep
            calls.append(_call(state, est_arr, est_dep, "predicted", delay_class))
    return replace(instance, calls=tuple(calls))


def monotone(instance: Instance) -> Instance:
    """The final monotone repair (BASE_PLAN.md 20.6): emitted times never decrease along the
    trip. Sources measured to a minute and positions can disagree by seconds; the later time
    holds. Recorded events in history are left as they are."""

    latest: Instant | None = None
    calls: list[CallState] = []
    for state in instance.calls:
        arrival, departure = state.estimated_arrival, state.estimated_departure
        if arrival is not None:
            if latest is not None and arrival < latest:
                arrival = latest
            latest = arrival
        if departure is not None:
            if latest is not None and departure < latest:
                departure = latest
            latest = departure
        if (arrival, departure) != (state.estimated_arrival, state.estimated_departure):
            state = replace(state, estimated_arrival=arrival, estimated_departure=departure)
        calls.append(state)
    return replace(instance, calls=tuple(calls))


def _departure(
    arrival: Instant | None,
    sched_arr: Instant | None,
    sched_dep: Instant | None,
    lateness: timedelta,
    floor: Instant | None,
    mode: str,
    policy: PredictionPolicy,
) -> tuple[Instant | None, timedelta]:
    """The predicted departure and the lateness carried on from it.

    Late into a real dwell, the vehicle leaves after the minimum dwell but not before its
    scheduled departure, so the dwell absorbs lateness. Otherwise the lateness carries over."""

    if sched_dep is None:
        return None, lateness
    dwell = sched_dep - sched_arr if sched_arr is not None else timedelta(0)
    if arrival is None or lateness <= timedelta(0) or dwell <= timedelta(0):
        return _shifted(sched_dep, lateness, floor), lateness
    long = dwell >= timedelta(seconds=policy.long_dwell_s(mode))
    minimum = policy.min_long_dwell_s(mode) if long else policy.min_dwell_s(mode)
    leaves = max(sched_dep, arrival + min(dwell, timedelta(seconds=minimum)))
    if floor is not None and leaves < floor:
        leaves = floor
    return Instant(leaves), leaves - sched_dep


def _passed(when: Instant | None, recorded: Interval | None) -> Instant | None:
    if when is not None:
        return when
    return None if recorded is None else _middle(recorded)


def _shifted(scheduled: Instant | None, delay: timedelta, floor: Instant | None) -> Instant | None:
    if scheduled is None:
        return None
    value = Instant(scheduled + delay)
    return floor if floor is not None and value < floor else value
