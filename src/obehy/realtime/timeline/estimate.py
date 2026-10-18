"""Per-call public model: estimate, status and source class (docs/R1_SLICE.md section 1).

- A call with a recorded event is `actual`. One passed inside a reception gap is `inferred`
  (realtime only: history has no event for it). Both take the crossing time interpolated
  between the fixes either side. A call the vehicle has arrived at but not yet left gets a
  predicted departure.
- A call behind progress without a crossing (passed before the journey was first seen) is
  `no_realtime`.
- A call ahead of progress is `predicted` from the current lateness: the tracker's, measured
  from GPS against the timetable, else the source's own delay; with neither it is `scheduled`.
  Early running counts only once observed: a vehicle standing at a stop ahead of its time
  (typically arriving into padded layover time) is waiting, so its lateness counts as zero.
  During a reception gap the last lateness holds; the vehicle is never assumed back on time.

A call the timetable gives no dwell (arrival = departure) still has one inside the minute.
Time the vehicle is seen standing there, up to the mode's `dwell_credit_s`, is not lateness:
while it stands, the lateness carried on is its lateness on arrival (never below zero: arriving
early it waits) plus any standing beyond the credit; after it leaves, the credit fades out
over the next leg, gone at the next call (`dwell_credit`). Most road stops are request stops,
so only a stop seen happening counts; a train certainly stops at a call not on request, so
there the lateness at the previous call stands in for an arrival not observed.

Propagation (BASE_PLAN.md section 20.5) carries the lateness call by call. A late vehicle
recovers time only where the timetable has a real dwell (departure after arrival): it leaves
after the minimum dwell, never before the scheduled departure. Early running holds to the next
call and fades beyond it (the mode's `early_keep` of the lead is kept at each further call:
early vehicles mostly lose their lead within a call or two), and a predicted departure from
a passenger stop is at most the mode's `max_early_departure_s` ahead of the timetable: trains
wait for their time (0); for road vehicles, whether one waits is a habit of particular stops,
learned from history later. An observed departure is never changed. Predictions never
precede an earlier call's estimate or the last fix (monotone repair). There is no uncertainty:
each estimate is the best single prediction.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from obehy.realtime.index import Trip
from obehy.realtime.model import CallState, CallStatus, Instance, Interval, SourceClass
from obehy.realtime.policy import PredictionPolicy
from obehy.realtime.timeline.plan import Plan, Scheduled
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
    if (
        state.estimated_arrival == arrival
        and state.estimated_departure == departure
        and state.status == status
        and state.source_class == source_class
    ):
        return state  # unchanged: the same object, so emitters can reuse what they made of it
    return state.estimated(arrival, departure, status, source_class)


def _with_calls(instance: Instance, calls: list[CallState]) -> Instance:
    if all(new is old for new, old in zip(calls, instance.calls, strict=True)):
        return instance
    return instance.evolve(calls=tuple(calls))


Lateness = tuple[timedelta | None, SourceClass | None]


def waiting_is_not_early(instance: Instance, plan: Plan, current: Lateness) -> Lateness:
    """The current lateness, with early running measured while the vehicle stands at a stop
    taken as zero: ahead of the timetable there it waits, it is not seen running early."""

    lateness, source_class = current
    track = instance.track
    if lateness is None or lateness >= timedelta(0) or source_class != "gps":
        return current
    if track is None or not track.hypotheses or not plan.at_stop(track.hypotheses[0].along_m):
        return current
    return timedelta(0), source_class


def _zero_dwell(scheduled: tuple[Instant | None, Instant | None]) -> bool:
    arrival, departure = scheduled
    return arrival is not None and arrival == departure


def _standing(lateness: timedelta, arrived: timedelta, credit: timedelta) -> timedelta:
    """The lateness carried on from a vehicle standing at a call without a timetabled dwell:
    standing up to `credit` beyond its arrival (or beyond the timetable, arriving early) is the
    dwell the minute holds."""

    growth = lateness - max(arrived, timedelta(0))
    return lateness - min(max(growth, timedelta(0)), credit)


def dwell_credit(
    instance: Instance, trip: Trip, plan: Plan, current: Lateness, policy: PredictionPolicy
) -> Lateness:
    """The current lateness less the dwell credited at the call last left, if it had no
    timetabled dwell, fading out along the leg to the next call's arrival trigger."""

    lateness, source_class = current
    track, progress = instance.track, instance.progress
    if lateness is None or source_class != "gps" or track is None or not track.hypotheses:
        return current
    if progress is None or not 0 <= progress.call_index < len(instance.calls) - 1:
        return current
    i = progress.call_index
    state = instance.calls[i]
    if not state.passenger or not _zero_dwell(plan.scheduled[i]):
        return current
    scheduled = plan.scheduled[i][1]
    left = _passed(state.passed_departure, state.departure)
    if scheduled is None or left is None:
        return current
    arrived_at = _passed(state.passed_arrival, state.arrival)
    if arrived_at is not None:
        arrived = arrived_at - scheduled
    elif instance.parts and not trip.calls[i].on_request and i > 0:
        # It certainly stopped: the lateness it came with, at the previous call's latest time.
        before = instance.calls[i - 1]
        arrival_before, departure_before = plan.scheduled[i - 1]
        left_before = _passed(before.passed_departure, before.departure)
        reached_before = _passed(before.passed_arrival, before.arrival)
        if left_before is not None and departure_before is not None:
            arrived = left_before - departure_before
        elif reached_before is not None and arrival_before is not None:
            arrived = reached_before - arrival_before
        else:
            return current
    else:
        return current  # a request stop not seen happening may have been passed
    credit = timedelta(seconds=policy.dwell_credit_s(instance.mode))
    credited = (left - scheduled) - _standing(left - scheduled, arrived, credit)
    if credited <= timedelta(0):
        return current
    start = plan.zones[i].departure_m
    end = plan.zones[i + 1].arrival_m
    if start is None or end is None or end <= start:
        return current
    share = min(1.0, max(0.0, (track.hypotheses[0].along_m - start) / (end - start)))
    return lateness - credited * (1.0 - share), source_class


def own_lateness(instance: Instance) -> Lateness:
    track = instance.track
    lateness = track.lateness_s if track is not None else None
    if lateness is not None:
        return timedelta(seconds=round(lateness)), "gps"
    if instance.delay_s is not None:
        return timedelta(seconds=instance.delay_s), "source"
    return None, None


def estimate(
    instance: Instance,
    scheduled: Scheduled,
    policy: PredictionPolicy,
    *,
    current: Lateness | None = None,
    anchor: tuple[int, timedelta] | None = None,
) -> Instance:
    """Per-call estimates; computed when state is emitted, not on every fix.

    Rail runs pass the fused current lateness and, from a source's prediction, the lateness at
    one call ahead (`anchor`) from which propagation continues (`timeline.fusion`)."""

    lateness, delay_class = current if current is not None else own_lateness(instance)
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
    ahead = 0  # passenger calls predicted so far
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
                if (
                    state.passenger
                    and delay_class == "gps"
                    and est_arr is not None
                    and sched_arr is not None
                    and _zero_dwell((sched_arr, sched_dep))
                ):
                    credit = timedelta(seconds=policy.dwell_credit_s(instance.mode))
                    lateness = _standing(lateness, est_arr - sched_arr, credit)
                est_dep, lateness = _departure(
                    est_arr,
                    sched_arr,
                    sched_dep,
                    lateness,
                    leave_floor,
                    instance.mode,
                    policy,
                    passenger=state.passenger,
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
            elif state.passenger and lateness < timedelta(0) and ahead > 0:
                # An early lead holds to the next call and fades beyond it: early vehicles
                # mostly lose it within a call or two (they wait, or drive slower).
                lateness = lateness * policy.early_keep(instance.mode)
            if state.passenger:
                ahead += 1
            est_arr = _shifted(sched_arr, lateness, floor)
            if est_arr is not None:
                floor = est_arr
            est_dep, lateness = _departure(
                est_arr,
                sched_arr,
                sched_dep,
                lateness,
                floor,
                instance.mode,
                policy,
                passenger=state.passenger,
            )
            if est_dep is not None:
                floor = est_dep
            calls.append(_call(state, est_arr, est_dep, "predicted", delay_class))
    return _with_calls(instance, calls)


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
    return _with_calls(instance, calls)


def _departure(
    arrival: Instant | None,
    sched_arr: Instant | None,
    sched_dep: Instant | None,
    lateness: timedelta,
    floor: Instant | None,
    mode: str,
    policy: PredictionPolicy,
    *,
    passenger: bool,
) -> tuple[Instant | None, timedelta]:
    """The predicted departure and the lateness carried on from it.

    Late into a real dwell, the vehicle leaves after the minimum dwell but not before its
    scheduled departure, so the dwell absorbs lateness. Early, it leaves a passenger stop at
    most `max_early_departure_s` ahead of time (the rest of the lead is lost there). Otherwise
    the lateness carries over."""

    if sched_dep is None:
        return None, lateness
    if passenger:
        early = -timedelta(seconds=policy.max_early_departure_s(mode))
        lateness = max(lateness, early)
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
