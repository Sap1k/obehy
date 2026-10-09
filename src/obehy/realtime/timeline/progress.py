"""Progress along a trip's path as map matching over a beam of hypotheses.

`BASE_PLAN.md` section 20.4 and `docs/R1_SLICE.md` section 9. A fix is never matched on its own:
each fix extends every live hypothesis to each place on the path it may be (one candidate per
segment), scored by three terms:

- geometry fit: lateral distance against the trust of the path there (a real shape closely, a
  stop-to-stop chord less, the longer the less), plus the heading when the fix has a bearing;
- forward speed: no backward move beyond GPS jitter, no move faster than the mode allows;
- change of lateness: time lost or gained against the timetable between the two fixes, with a
  spread growing with the time between them. Absolute lateness is never penalised.

The most likely hypothesis is the live position. A crossing is committed only when every
surviving hypothesis has passed its trigger, so out-and-back branches (závleky), loops and stops
passed before being served resolve from the following fixes instead of being guessed. A
committed crossing always gets a time for realtime; it is recorded as an event (history) only
when the fixes either side are close enough to say when it happened.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from itertools import pairwise

from obehy.realtime.model import EventKind, Hypothesis, Interval, Position, Progress, Track
from obehy.realtime.policy import Policy, ProgressPolicy
from obehy.realtime.timeline.path import Candidate, Path, Plan, Timetable, candidates
from obehy.realtime.times import Instant

# (call index, kind, fixes either side, interpolated time, recorded as an event)
Crossing = tuple[int, EventKind, Interval, Instant, bool]


@dataclass(frozen=True, slots=True)
class Step:
    track: Track
    progress: Progress | None
    crossed: tuple[Crossing, ...]
    off_route_since: Instant | None
    off_route: bool


# --- where the vehicle is ------------------------------------------------------------------------


def call_index_at(plan: Plan, along_m: float) -> int:
    """The last call the vehicle has left: its departure trigger (or, for the last call, its
    arrival trigger) is at or behind `along_m`; -1 before the first departure."""

    index = -1
    for i, (arrival, departure) in enumerate(plan.triggers):
        trigger = departure if departure is not None else arrival
        if trigger is not None and trigger <= along_m:
            index = i
    return index


# --- lateness ------------------------------------------------------------------------------------


def lateness_at(at: Instant, along: float, timetable: Timetable) -> float:
    """Seconds behind (positive) or ahead of the timetable; 0 inside a call's dwell."""

    window = timetable(along)
    if window is None:
        return 0.0
    lo, hi = window
    if lo is not None and at < lo:
        return (at - lo).total_seconds()
    if hi is not None and at > hi:
        return (at - hi).total_seconds()
    return 0.0


def _planned(at: Instant, along: float, timetable: Timetable) -> Instant | None:
    """The timetable time of a fix: its time clamped into the window of its place."""

    window = timetable(along)
    if window is None:
        return None
    lo, hi = window
    if lo is not None and at < lo:
        return lo
    if hi is not None and at > hi:
        return hi
    return at


# --- scoring -------------------------------------------------------------------------------------


def _sigma(policy: ProgressPolicy) -> Callable[[Path, float], float]:
    def sigma(path: Path, segment_m: float) -> float:
        geometry = (
            policy.shape_sigma_m
            if path.shaped
            else min(
                policy.chord_max_sigma_m,
                max(policy.chord_min_sigma_m, policy.chord_k * segment_m),
            )
        )
        return math.hypot(policy.gps_sigma_m, geometry)

    return sigma


def _emission(candidate: Candidate, position: Position, path: Path, p: ProgressPolicy) -> float:
    score = -0.5 * (candidate.lateral_m / candidate.sigma_m) ** 2 - math.log(candidate.sigma_m)
    if position.bearing is not None:
        turn = abs((position.bearing - candidate.heading_deg + 180.0) % 360.0 - 180.0)
        spread = p.bearing_sigma_deg if path.shaped else p.chord_bearing_sigma_deg
        score -= 0.5 * (turn / spread) ** 2
    return score


def _transition(
    h: Hypothesis,
    along: float,
    at: Instant,
    lateness: float,
    speed: float,
    p: ProgressPolicy,
) -> float | None:
    """Log-likelihood of moving from `h` to `along` at `at`; None if impossible.

    The speed limit allows only GPS jitter as slack, never the geometry's uncertainty: a loose
    chord widens where a fix may lie, not how far the vehicle may travel."""

    moved = along - h.along_m
    elapsed = (at - h.at).total_seconds()
    if elapsed <= 0:
        return None
    if max(0.0, moved) - p.jitter_m > speed * elapsed:
        return None
    change = lateness - h.lateness_s
    if change >= 0:
        spread = p.loss_sigma_base_s + p.loss_sigma_rate * elapsed
    else:
        spread = p.gain_sigma_base_s + p.gain_sigma_rate * elapsed
    return -0.5 * (change / spread) ** 2


# --- one fix -------------------------------------------------------------------------------------


def update(
    plan: Plan,
    track: Track | None,
    position: Position,
    at: Instant,
    mode: str,
    policy: Policy,
) -> Step:
    p = policy.progress
    path, timetable = plan.path, plan.timetable
    if len(path.points) < 2:
        return Step(track or Track((), 0.0), None, (), None, False)
    speed = p.max_speed_mps(mode)
    live = track.hypotheses if track is not None else ()
    window = None
    if live:
        elapsed = max((at - h.at).total_seconds() for h in live)
        reach = p.reach_sigmas * math.hypot(p.gps_sigma_m, p.chord_min_sigma_m)
        window = (
            min(h.along_m for h in live) - p.jitter_m,
            max(h.along_m for h in live) + speed * max(0.0, elapsed) + reach,
        )
    found = candidates(path, position.lon, position.lat, _sigma(p), p.reach_sigmas, window)

    extended: list[Hypothesis] = []
    for candidate in found:
        fit = _emission(candidate, position, path, p)
        if not live:
            lateness = lateness_at(at, candidate.along_m, timetable)
            prior = -0.5 * (lateness / p.start_lateness_sigma_s) ** 2
            history = ((candidate.along_m, at),)
            extended.append(Hypothesis(candidate.along_m, at, lateness, fit + prior, history))
            continue
        best: Hypothesis | None = None
        for h in live:
            if candidate.along_m < h.along_m - p.jitter_m:
                continue  # backwards beyond GPS noise: impossible
            along = max(candidate.along_m, h.along_m)  # a small step back is no movement
            lateness = lateness_at(at, along, timetable)
            move = _transition(h, along, at, lateness, speed, p)
            if move is None:
                continue
            score = h.log_p + move + fit
            if best is None or score > best.log_p:
                best = Hypothesis(along, at, lateness, score, (*h.history, (along, at)))
        if best is not None:
            extended.append(best)
    # A fix the geometry does not explain (a detour, a road far from a stop-to-stop chord) says
    # nothing about where the vehicle is: each hypothesis may also hold as if the fix were
    # missing, at a fixed cost, instead of dying for it.
    for h in live:
        since = h.off_path_since or at
        held = h.log_p + p.off_path_log_p
        extended.append(Hypothesis(h.along_m, h.at, h.lateness_s, held, h.history, since))

    if not extended:
        return _unmatched(track, at, policy)
    beam = _prune(extended, p)
    if track is not None and track.hypotheses:
        committed_m = track.committed_m
    else:
        committed_m = min(h.along_m for h in beam)  # nothing before the first fix is observed
    crossed_at = track.crossed_at if track is not None else None
    beam, committed_m, crossed = _commit(plan, beam, committed_m, crossed_at, at, policy)
    if crossed:
        crossed_at = crossed[-1][3]
    best = beam[0]
    progress = Progress(best.along_m, call_index_at(plan, best.along_m), at)
    since = best.off_path_since
    off_route = since is not None and at - since >= timedelta(seconds=p.off_route_hold_s)
    track = Track(beam, committed_m, seen_at=at, crossed_at=crossed_at)
    return Step(track, progress, crossed, since, off_route)


def _prune(extended: list[Hypothesis], p: ProgressPolicy) -> tuple[Hypothesis, ...]:
    """Most likely first; near-identical positions merged; at most `beam`, none far behind."""

    ordered = sorted(extended, key=lambda h: (-h.log_p, h.along_m, h.off_path_since is not None))
    top = ordered[0].log_p
    kept: list[Hypothesis] = []
    for h in ordered:
        if top - h.log_p > p.prune or len(kept) >= p.beam:
            break
        if any(abs(h.along_m - k.along_m) < 1.0 for k in kept):
            continue
        kept.append(
            Hypothesis(h.along_m, h.at, h.lateness_s, h.log_p - top, h.history, h.off_path_since)
        )
    return tuple(kept)


Fixes = tuple[float, Instant, float, Instant]  # (along, time) of the fixes either side


def _commit(
    plan: Plan,
    beam: tuple[Hypothesis, ...],
    committed_m: float,
    crossed_at: Instant | None,
    at: Instant,
    policy: Policy,
) -> tuple[tuple[Hypothesis, ...], float, tuple[Crossing, ...]]:
    """Commit, in path order, every crossing all surviving hypotheses agree on.

    Agreement means each hypothesis that is not negligible (within `agree_within` of the most
    likely) has passed the trigger *and* brackets the crossing with the same two fixes (the
    common prefix of their histories); negligible ones that disagree are dropped once it is
    committed. The first disagreement stops
    committing: that crossing is still undecided. A crossing before every history starts was
    never observed and gets nothing. One bracketed by fixes further apart than
    `max_event_interval_s` (a reception gap) gets a time for realtime but is not recorded as an
    event. A decision open longer than `max_commit_lag_s` keeps only the most likely
    hypothesis, which then agrees with itself. Crossing times never decrease along the path:
    each is at least the one committed before it (`crossed_at`).
    """

    p = policy.progress
    if not beam:
        return beam, committed_m, ()
    kept = tuple(h for h in beam if h.log_p >= -p.agree_within)
    oldest = min(h.history[0][1] for h in kept)
    if len(kept) > 1 and at - oldest > timedelta(seconds=p.max_commit_lag_s):
        kept = (kept[0],)
    frontier = min(h.along_m for h in kept)
    first = bisect_right(plan.event_distances, committed_m)
    last = bisect_right(plan.event_distances, frontier)
    pending = plan.events[first:last]
    longest = timedelta(seconds=p.max_event_interval_s)
    crossed: list[Crossing] = []
    reached = committed_m
    for trigger, i, rank in pending:
        kind: EventKind = "arrival" if rank == 0 else "departure"
        found = [_bracket(h.history, trigger) for h in kept]
        if len({None if f is None else (f[1], f[3]) for f in found}) > 1:
            break  # the readings disagree on when: undecided
        reached = trigger
        fixes = found[0]
        if fixes is not None:
            arrival, departure = plan.scheduled[i]
            if kind == "arrival":
                planned = arrival if arrival is not None else departure
            else:
                planned = departure if departure is not None else arrival
            when = _crossing_time(fixes, trigger, planned, plan.timetable)
            if crossed_at is not None and when < crossed_at:
                when = crossed_at
            crossed_at = when
            interval = Interval(fixes[1], fixes[3])
            recorded = interval.hi - interval.lo <= longest
            crossed.append((i, kind, interval, when, recorded))
    if reached == committed_m:
        return beam, committed_m, ()
    survivors = (h for h in beam if h in kept or h.along_m >= reached)
    return tuple(_cut(h, reached) for h in survivors), reached, tuple(crossed)


def _bracket(history: tuple[tuple[float, Instant], ...], trigger: float) -> Fixes | None:
    """The fixes either side of where the history crossed `trigger`; None if it was crossed
    before the history starts (never observed)."""

    for (a0, t0), (a1, t1) in pairwise(history):
        if a0 < trigger <= a1:
            return a0, t0, a1, t1
    return None


def _crossing_time(
    fixes: Fixes, trigger: float, planned: Instant | None, timetable: Timetable
) -> Instant:
    """When the vehicle crossed `trigger` between two fixes.

    Lateness drifts between fixes, so the crossing is placed in proportion to timetable time,
    not distance: a vehicle that waited at a stop and then drove on is placed by the dwell, and
    across a reception gap the timetable's pace between the fixes is kept. Without timetable
    times it falls back to distance."""

    a0, t0, a1, t1 = fixes
    s0, s1 = _planned(t0, a0, timetable), _planned(t1, a1, timetable)
    if planned is not None and s0 is not None and s1 is not None and s1 > s0:
        share = (planned - s0) / (s1 - s0)
    else:
        share = (trigger - a0) / (a1 - a0)
    return Instant(t0 + (t1 - t0) * min(1.0, max(0.0, share)))


def _cut(h: Hypothesis, frontier: float) -> Hypothesis:
    """Drop history before the last fix at or behind the commit point."""

    start = 0
    for i, (along, _) in enumerate(h.history):
        if along <= frontier:
            start = i
    if start == 0:
        return h
    return Hypothesis(h.along_m, h.at, h.lateness_s, h.log_p, h.history[start:], h.off_path_since)


def _unmatched(track: Track | None, at: Instant, policy: Policy) -> Step:
    """A fix before any hypothesis exists that matches no segment: nothing to track yet."""

    if track is None:
        track = Track((), 0.0)
    since = track.unmatched_since or at
    held = at - since >= timedelta(seconds=policy.progress.off_route_hold_s)
    return Step(replace(track, unmatched_since=since, seen_at=at), None, (), since, held)
