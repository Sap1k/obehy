"""Committing crossings of trigger points once the tracker's readings agree.

`BASE_PLAN.md` section 20.4 ("Commit when all agree") and `docs/R1_SLICE.md` section 9, step 6.
A crossing is committed in path order when every reading that is not negligible has passed the
trigger between the same two fixes. Its time is placed between those fixes by timetable time;
it is recorded as an event (history) only when the fixes are close enough to say when it
happened, and realtime gets its time either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from typing import NamedTuple

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
    recorded: bool  # close enough fixes: an event for history too


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
    undecided. A crossing before every history starts was never observed and gets nothing. One
    bracketed by fixes further apart than `max_event_interval_s` (a reception gap) gets a time
    for realtime but is not recorded as an event. Crossing times never decrease along the path:
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
        reached = trigger.along_m
        bracket = brackets[0]
        if bracket is None:
            continue  # crossed before anything was observed
        when = _crossing_time(bracket, trigger, plan)
        if crossed_at is not None and when < crossed_at:
            when = crossed_at
        crossed_at = when
        fixes = Interval(bracket.before.at, bracket.after.at)
        recorded = fixes.hi - fixes.lo <= longest
        crossed.append(Crossing(trigger.call, trigger.kind, fixes, when, recorded))
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


def _cut(h: Hypothesis, frontier: float) -> Hypothesis:
    """Drop history before the last fix at or behind the commit point."""

    start = 0
    for i, placement in enumerate(h.history):
        if placement.along_m <= frontier:
            start = i
    return h if start == 0 else h.evolve(history=h.history[start:])
