"""Committing crossings of trigger points once the tracker's readings agree.

`BASE_PLAN.md` section 20.4 ("Commit when all agree") and `docs/R1_SLICE.md` section 9, step 6.
A crossing is committed in path order when every reading that is not negligible has passed the
trigger between the same two fixes. Its time is placed between those fixes by timetable time;
it is recorded as an event (history) when the fixes are close enough to say when it happened,
or, further apart, as an inferred one when the vehicle was about as late at both (then the
timetable says when it passed); realtime gets its time either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from typing import Literal, NamedTuple

from obehy.realtime.model import EventKind, Hypothesis, Interval, Placement
from obehy.realtime.policy import ProgressPolicy
from obehy.realtime.timeline.plan import Plan, Timetable, Trigger
from obehy.realtime.times import Instant


@dataclass(frozen=True, slots=True)
class Crossing:
    """A committed crossing of a call's trigger point."""

    call: int
    kind: EventKind
    fixes: Interval  # the fixes either side
    at: Instant  # when, placed between them
    # An event for history too: observed between close enough fixes (`progress`, its interval
    # the fixes), or inferred across a gap the lateness agrees on either side (`inferred`, its
    # interval the times that lateness allows); None: realtime only.
    method: Literal["progress", "inferred"] | None
    interval: Interval | None


@dataclass(frozen=True, slots=True)
class Committed:
    beam: tuple[Hypothesis, ...]
    committed_m: float
    crossed: tuple[Crossing, ...]
    crossed_at: Instant | None


class Bracket(NamedTuple):
    """The fixes either side of where a reading crossed a point."""

    before: Placement
    after: Placement


def commit(
    plan: Plan,
    beam: tuple[Hypothesis, ...],
    committed_m: float,
    crossed_at: Instant | None,
    at: Instant,
    policy: ProgressPolicy,
) -> Committed:
    """Commit, in path order, every crossing the deciding readings agree on.

    The deciding readings are those within `agree_within` of the most likely. Agreement means
    each has passed the trigger *and* brackets the crossing with the same two fixes (the common
    prefix of their histories). The first disagreement stops committing: that crossing is still
    undecided, as is one a reading held off the path has passed with no fix past it yet. A
    crossing before every history starts was never observed and gets nothing. One
    bracketed by fixes further apart than `max_event_interval_s` (a reception gap, a stretch off
    the path) is recorded as inferred when the lateness at the two fixes differs by at most
    `max_inferred_spread_s`, else it gets a time for realtime only. Crossing times never
    decrease along the path:
    each is at least the one committed before it (`crossed_at`). Readings outside the decision
    that are behind the new commit point contradict it and are dropped.
    """

    if not beam:
        return Committed(beam, committed_m, (), crossed_at)
    deciding = _deciding(beam, at, policy)
    frontier = min(h.along_m for h in deciding)
    longest = timedelta(seconds=policy.max_event_interval_s)
    crossed: list[Crossing] = []
    reached = committed_m
    for trigger in plan.triggers_between(committed_m, frontier):
        brackets = [_bracket(h.history, trigger.along_m) for h in deciding]
        if len({_fix_times(b) for b in brackets}) > 1:
            break  # the readings disagree on when: undecided
        bracket = brackets[0]
        if bracket is None and any(_before(h.history, trigger.along_m) for h in deciding):
            break  # passed by a reading held off the path: no fix past it yet
        reached = trigger.along_m
        if bracket is None:
            # Crossed before anything was observed: nothing, except the origin's departure
            # just before the first fix, inferred from the lateness there.
            first = deciding[0].history[0]
            origin = trigger.call == 0 and trigger.kind == "departure"
            inferred = _before_first(first, trigger, plan, policy) if origin else None
            if inferred is not None:
                when = Instant(inferred.lo + (inferred.hi - inferred.lo) / 2)
                if crossed_at is not None and when < crossed_at:
                    when = crossed_at
                crossed_at = when
                fixes = Interval(first.at, first.at)
                crossed.append(
                    Crossing(trigger.call, trigger.kind, fixes, when, "inferred", inferred)
                )
            continue
        when = _crossing_time(bracket, trigger, plan)
        if crossed_at is not None and when < crossed_at:
            when = crossed_at
        crossed_at = when
        fixes = Interval(bracket.before.at, bracket.after.at)
        if fixes.hi - fixes.lo <= longest:
            crossed.append(Crossing(trigger.call, trigger.kind, fixes, when, "progress", fixes))
            continue
        inferred = _inferred(bracket, trigger, when, plan, policy)
        method = None if inferred is None else "inferred"
        crossed.append(Crossing(trigger.call, trigger.kind, fixes, when, method, inferred))
    if reached == committed_m:
        return Committed(beam, committed_m, (), crossed_at)
    survivors = (h for h in beam if h in deciding or h.along_m >= reached)
    beam = tuple(_cut(h, reached) for h in survivors)
    return Committed(beam, reached, tuple(crossed), crossed_at)


def _deciding(
    beam: tuple[Hypothesis, ...], at: Instant, policy: ProgressPolicy
) -> tuple[Hypothesis, ...]:
    """The readings a commit needs to agree: all but the negligible ones, or only the most
    likely once a decision has been open longer than `max_commit_lag_s`."""

    deciding = tuple(h for h in beam if h.log_p >= -policy.agree_within)
    oldest = min(h.history[0].at for h in deciding)
    if len(deciding) > 1 and at - oldest > timedelta(seconds=policy.max_commit_lag_s):
        return deciding[:1]
    return deciding


def _fix_times(bracket: Bracket | None) -> tuple[Instant, Instant] | None:
    return None if bracket is None else (bracket.before.at, bracket.after.at)


def _bracket(history: tuple[Placement, ...], along_m: float) -> Bracket | None:
    """The fixes either side of where the history crossed `along_m`; None if it was crossed
    before the history starts (never observed)."""

    for before, after in pairwise(history):
        if before.along_m < along_m <= after.along_m:
            return Bracket(before, after)
    return None


def _crossing_time(bracket: Bracket, trigger: Trigger, plan: Plan) -> Instant:
    """When the vehicle crossed `trigger` between two fixes.

    Lateness drifts between fixes, so the crossing is placed in proportion to timetable time,
    not distance: a vehicle that waited at a stop and then drove on is placed by the dwell, and
    across a reception gap the timetable's pace between the fixes is kept. Without timetable
    times it falls back to distance."""

    before, after = bracket
    timetable: Timetable = plan.timetable
    planned = plan.scheduled_time(trigger.call, trigger.kind)
    s0 = timetable.planned(before.at, before.along_m)
    s1 = timetable.planned(after.at, after.along_m)
    if planned is not None and s0 is not None and s1 is not None and s1 > s0:
        share = (planned - s0) / (s1 - s0)
    else:
        share = (trigger.along_m - before.along_m) / (after.along_m - before.along_m)
    return Instant(before.at + (after.at - before.at) * min(1.0, max(0.0, share)))


def _inferred(
    bracket: Bracket, trigger: Trigger, when: Instant, plan: Plan, policy: ProgressPolicy
) -> Interval | None:
    """When a crossing between fixes too far apart to observe it happened, if the vehicle was
    about as late at both: the scheduled time shifted by the lateness either side (within the
    fixes, and holding the placed time `when`). None if the lateness differs by more than
    `max_inferred_spread_s` (it was made up or lost somewhere unknown in between) or the call has
    no scheduled time."""

    planned = plan.scheduled_time(trigger.call, trigger.kind)
    if planned is None:
        return None
    timetable = plan.timetable
    before, after = bracket
    least, most = sorted(
        (
            timetable.lateness_s(before.at, before.along_m),
            timetable.lateness_s(after.at, after.along_m),
        )
    )
    if most - least > policy.max_inferred_spread_s:
        return None
    lo = max(before.at, min(when, planned + timedelta(seconds=least)))
    hi = min(after.at, max(when, planned + timedelta(seconds=most)))
    return Interval(Instant(lo), Instant(hi)) if lo <= hi else Interval(when, when)


def terminus(plan: Plan, history: tuple[Placement, ...], policy: ProgressPolicy) -> Interval | None:
    """When a vehicle last seen just short of its terminus arrived there: the scheduled arrival
    shifted by the lateness at its last fixes (their range), if the last is at most
    `max_extrapolate_s` of timetable time before it and the lateness over the fixes kept (at
    most `max_inferred_spread_s` apart). From the last fix on; None otherwise."""

    last = len(plan.scheduled) - 1
    planned = plan.scheduled_time(last, "arrival")
    if planned is None or not history:
        return None
    fix = history[-1]
    reached = plan.timetable.planned(fix.at, fix.along_m)
    if reached is None or (planned - reached).total_seconds() > policy.max_extrapolate_s:
        return None
    lateness = [plan.timetable.lateness_s(p.at, p.along_m) for p in history]
    if max(lateness) - min(lateness) > policy.max_inferred_spread_s:
        return None
    lo = max(fix.at, planned + timedelta(seconds=min(lateness)))
    hi = max(fix.at, planned + timedelta(seconds=max(lateness)))
    return Interval(Instant(lo), Instant(hi))


def _before_first(
    first: Placement, trigger: Trigger, plan: Plan, policy: ProgressPolicy
) -> Interval | None:
    """When the vehicle left its origin, first seen just past it: the scheduled departure
    shifted by the lateness at the first fix, if that fix is at most `max_extrapolate_s` of
    timetable time past it. Between the scheduled departure (or earlier, running early) and the
    first fix; None without a scheduled time or further away."""

    planned = plan.scheduled_time(trigger.call, trigger.kind)
    reached = plan.timetable.planned(first.at, first.along_m)
    if planned is None or reached is None:
        return None
    if (reached - planned).total_seconds() > policy.max_extrapolate_s:
        return None
    lateness = timedelta(seconds=plan.timetable.lateness_s(first.at, first.along_m))
    lo = min(planned, planned + lateness)
    hi = min(first.at, planned + max(lateness, timedelta(0)))
    return Interval(Instant(min(lo, hi)), Instant(hi))


def _before(history: tuple[Placement, ...], along_m: float) -> bool:
    """Whether the history was observed before `along_m` but not yet past it."""

    return bool(history) and history[0].along_m < along_m and history[-1].along_m < along_m


def _cut(h: Hypothesis, frontier: float) -> Hypothesis:
    """Drop history before the last fix at or behind the commit point."""

    start = 0
    for i, placement in enumerate(h.history):
        if placement.along_m <= frontier:
            start = i
    return h if start == 0 else h.evolve(history=h.history[start:])
