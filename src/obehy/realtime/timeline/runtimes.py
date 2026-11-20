"""Hop running times learned live (BASE_PLAN.md section 20.5).

A hop is two consecutive passenger calls, keyed by mode and the two locations, so every line
over the same stops shares it. Each journey that records an arrival at its end adds a traversal:
the time from its recorded arrival at the first call (its departure at the origin, which has no
arrival) against the timetable's time for the same span. The running time of a hop now is the
timetable's times a ratio: the traversals' ratios weighted by recency (decaying by e every
`runtime_decay_s`), shrunk toward the timetable's own (1) with `runtime_prior_weight`, so one
traversal moves it only part of the way and none leaves the timetable. In the Prague morning
peak buses lose time on every hop, and a lateness carried forward unchanged predicts them
earlier the further ahead (2026-10-15 06:45-09:00, `obehy rt replay --quality`).

A whole-minute timetable gives some hops no time at all; they get no ratio and keep the
timetable's (none).
"""

from __future__ import annotations

import math
from collections import deque
from datetime import timedelta

from obehy.realtime.model import CallState, HopKey, Instance, Traversal
from obehy.realtime.policy import PredictionPolicy
from obehy.realtime.timeline.plan import Plan, Scheduled
from obehy.realtime.times import Instant


def _previous_passenger(calls: tuple[CallState, ...], i: int) -> int | None:
    return next((k for k in range(i - 1, -1, -1) if calls[k].passenger), None)


def _key(instance: Instance, a: int, b: int) -> HopKey:
    calls = instance.calls
    return (instance.mode, calls[a].location_id, calls[b].location_id, a == 0)


def record(
    hops: dict[HopKey, deque[Traversal]],
    instance: Instance,
    before: tuple[CallState, ...],
    scheduled: Scheduled,
    policy: PredictionPolicy,
) -> None:
    """Add the hops `instance` completed since its calls were `before`: each passenger call with
    a newly recorded arrival, timed from the previous passenger call's recorded event."""

    window = timedelta(seconds=policy.runtime_keep_decays * policy.runtime_decay_s)
    calls = instance.calls
    for b, (old, new) in enumerate(zip(before, calls, strict=True)):
        if not new.passenger or new.arrival is None or old.arrival is not None:
            continue
        a = _previous_passenger(calls, b)
        if a is None:
            continue
        start = calls[a].departure if a == 0 else calls[a].arrival
        planned_start = scheduled[a][1] if a == 0 else (scheduled[a][0] or scheduled[a][1])
        planned_end = scheduled[b][0] or scheduled[b][1]
        if start is None or planned_start is None or planned_end is None:
            continue
        end = new.arrival.mid
        taken = (end - start.mid).total_seconds()
        planned = (planned_end - planned_start).total_seconds()
        if taken <= 0 or planned <= 0:
            continue
        traversals = hops.setdefault(_key(instance, a, b), deque())
        traversals.append((end, taken, planned))
        while traversals and traversals[0][0] < end - window:
            traversals.popleft()


def ratio(
    hops: dict[HopKey, deque[Traversal]], key: HopKey, now: Instant, policy: PredictionPolicy
) -> float | None:
    """The hop's running time against the timetable now: the traversals' ratios weighted by
    recency (`runtime_decay_s`), with the timetable's (1) weighing `runtime_prior_weight`; None
    for a hop no one has driven lately."""

    traversals = hops.get(key)
    if not traversals:
        return None
    decay = policy.runtime_decay_s
    most = policy.runtime_ratio_max
    total, weight = policy.runtime_prior_weight, policy.runtime_prior_weight
    for end, taken, planned in traversals:
        age = (now - end).total_seconds()
        if age < 0 or age > policy.runtime_keep_decays * decay:
            continue
        w = math.exp(-age / decay)
        total += w * min(most, max(1.0 / most, taken / planned))
        weight += w
    return total / weight


def forget(hops: dict[HopKey, deque[Traversal]], now: Instant, policy: PredictionPolicy) -> None:
    """Drop hops with no traversal in the window."""

    since = now - timedelta(seconds=policy.runtime_keep_decays * policy.runtime_decay_s)
    for key in [k for k, v in hops.items() if not v or v[-1][0] < since]:
        del hops[key]


def running_extra(
    instance: Instance,
    plan: Plan,
    hops: dict[HopKey, deque[Traversal]],
    now: Instant,
    policy: PredictionPolicy,
) -> tuple[timedelta, ...]:
    """Per call, the time the hop into it takes now beyond the timetable's running time (from
    the previous passenger call's scheduled departure); on the hop the vehicle is driving, only
    for the share of it left. Zero for calls behind progress and where there is no ratio."""

    calls = instance.calls
    extra = [timedelta(0)] * len(calls)
    progress = instance.progress
    reached = progress.call_index if progress is not None else -1
    for b in range(max(reached, 0) + 1, len(calls)):
        if not calls[b].passenger:
            continue
        a = _previous_passenger(calls, b)
        if a is None:
            continue
        r = ratio(hops, _key(instance, a, b), now, policy)
        start = plan.scheduled[a][1] or plan.scheduled[a][0]
        end = plan.scheduled[b][0] or plan.scheduled[b][1]
        if r is None or start is None or end is None or end <= start:
            continue
        share = 1.0
        if progress is not None and a <= reached:  # driving the hop: the share of it left
            left, reach = plan.zones[a].departure_m, plan.zones[b].arrival_m
            if left is not None and reach is not None and reach > left:
                share = min(1.0, max(0.0, (reach - progress.distance_m) / (reach - left)))
        extra[b] = (end - start) * ((r - 1.0) * share)
    return tuple(extra)


def minimum_drives(
    instance: Instance,
    plan: Plan,
    hops: dict[HopKey, deque[Traversal]],
    now: Instant,
    policy: PredictionPolicy,
    speed_mps: float,
) -> tuple[timedelta, ...]:
    """Per passenger call, the least time from the previous passenger call's departure to its
    arrival: the hop's running time now (timetable times the live ratio) less the typical dwell
    the timetable's minute holds (`dwell_credit_s`), and never less than the hop's length at the
    mode's top speed (two calls in one timetable minute are still a drive apart). Zero for the
    first call and non-passenger calls."""

    calls = instance.calls
    credit = timedelta(seconds=policy.dwell_credit_s(instance.mode))
    distances = plan.path.call_distances_m
    drives = [timedelta(0)] * len(calls)
    for b in range(1, len(calls)):
        if not calls[b].passenger:
            continue
        a = _previous_passenger(calls, b)
        if a is None:
            continue
        start = plan.scheduled[a][1] or plan.scheduled[a][0]
        end = plan.scheduled[b][0] or plan.scheduled[b][1]
        running = end - start if start is not None and end is not None else timedelta(0)
        r = ratio(hops, _key(instance, a, b), now, policy)
        if r is not None:
            running = running * r
        fastest = timedelta(seconds=max(0.0, distances[b] - distances[a]) / speed_mps)
        drives[b] = max(running - credit, fastest, timedelta(0))
    return tuple(drives)
