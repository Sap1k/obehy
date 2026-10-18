"""Progress along a trip's path as map matching over a beam of hypotheses.

`BASE_PLAN.md` section 20.4 and `docs/R1_SLICE.md` section 9. A fix is never matched on its own:
each fix extends every live hypothesis to each place on the path it may be (one candidate per
segment), scored by three terms:

- geometry fit: lateral distance against the trust of the path there (a real shape closely, a
  stop-to-stop chord less, the longer the less), plus the heading when the fix has a bearing;
- forward speed: no backward move beyond GPS jitter, no move faster than the mode allows;
  on a real shape, no move much shorter than the fixes moved apart;
- change of lateness: time lost or gained against the timetable between the two fixes, with a
  spread growing with the time between them. Absolute lateness is never penalised.

A fix the path does not explain holds a hypothesis at a fixed cost, advanced by the distance the
fixes moved (never into the next call's arrival trigger): where a shape misses the road taken,
or on a detour, the distance driven measures progress, so the delay neither freezes nor grows
from a projection that stands still. Two roads of about the same length differ in where they
go, not how far.
The most likely hypothesis is the live position; crossings are committed once the readings
agree (`commit.py`). GPS fixes are the only evidence in R1; source-reported progress (SŽ
passages, a next stop) will be scored against the same hypotheses as a further term.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta

from obehy.realtime.geo import distance_m
from obehy.realtime.model import Hypothesis, OffPath, Placement, Position, Progress, Track
from obehy.realtime.policy import ProgressPolicy
from obehy.realtime.timeline.commit import Crossing, commit
from obehy.realtime.timeline.path import Candidate, Path, candidates, point_at
from obehy.realtime.timeline.plan import Plan
from obehy.realtime.times import Instant, micros


@dataclass(frozen=True, slots=True)
class Step:
    """The tracker after one fix."""

    track: Track
    progress: Progress | None
    crossed: tuple[Crossing, ...]
    off_route_since: Instant | None
    off_route: bool


def update(
    plan: Plan,
    track: Track | None,
    position: Position,
    at: Instant,
    mode: str,
    source: str,
    policy: ProgressPolicy,
) -> Step:
    """Extend the journey's hypotheses with one fix (from `source`) and commit what they
    agree on."""

    if len(plan.path.points) < 2:
        return Step(track or Track((), 0.0), None, (), None, False)
    live = track.hypotheses if track is not None else ()
    speed = policy.max_speed_mps(mode)
    at_us = micros(at)
    window = _search_window(live, at_us, speed, policy)
    found = candidates(
        plan.path, position.lon, position.lat, _sigma(policy), policy.reach_sigmas, window
    )
    here = (position.lon, position.lat)
    # How far the fixes moved apart: progress on a real shape must keep up with it, and a held
    # reading advances by it. Only between fixes of one source: a run fuses sources of
    # different precision (DÚK GPS, the SŽ map).
    moved: float | None = None
    if track is not None and track.fix is not None and track.fix[0] == source:
        moved = distance_m(track.fix[1:], here)
    if live:
        extended = _extend(plan, live, found, position, at, at_us, speed, moved, policy)
        extended += _held(plan, live, at, at_us, moved, here, policy)
    else:
        extended = _start(plan, found, position, at, at_us, policy)
    if not extended:
        return _unmatched(track, at, policy)

    beam = _prune(extended, policy)
    # A restart never commits behind what was committed (a source's point anchor can leave a
    # track without readings; before any match nothing is committed yet).
    floor = _first_floor(beam)
    committed_m = track.committed_m if live and track is not None else floor
    if track is not None and not live:
        committed_m = max(track.committed_m, floor)
    crossed_at = track.crossed_at if track is not None else None
    done = commit(plan, beam, committed_m, crossed_at, at, policy)
    best = done.beam[0]
    progress = Progress(best.along_m, plan.call_index_at(best.along_m), at)
    since = best.off_path_since
    off_route = since is not None and at - since >= timedelta(seconds=policy.off_route_hold_s)
    previous = track.off_path if track is not None else None
    estimate = _chord_estimate(plan, best, previous, here, at, at_us, speed)
    new_track = Track(
        done.beam,
        done.committed_m,
        seen_at=at,
        crossed_at=done.crossed_at,
        off_path=estimate,
        fix=(source, *here),
    )
    return Step(new_track, progress, done.crossed, since, off_route)


def at_origin_or_moving(
    plan: Plan, before: Position | None, after: Position, at: Instant, policy: ProgressPolicy
) -> bool:
    """Whether a fix shows the vehicle at its first stop (before the departure trigger), or
    two consecutive fixes show it moving on along the path after it: forward by more than
    GPS jitter (a vehicle standing on a stand past the first stop is neither), and no further
    ahead of the timetable than the start prior allows (a vehicle driving to its start over
    roads the trip takes later, or rolling onto its stand, is not running half an hour early)."""

    first = plan.zones[0].departure_m if plan.zones else None
    after_m = plan.path.call_distances_m[0] if first is None else first
    sigma = _sigma(policy)
    now = candidates(plan.path, after.lon, after.lat, sigma, policy.reach_sigmas)
    if any(a.along_m <= after_m for a in now):
        return True
    if before is None:
        return False
    then = candidates(plan.path, before.lon, before.lat, sigma, policy.reach_sigmas)
    earliest = -policy.reach_sigmas * policy.start_early_sigma_s
    return any(
        b.along_m > after_m
        and a.along_m - b.along_m > policy.jitter_m
        and plan.timetable.lateness_s(at, a.along_m) >= earliest
        for b in then
        for a in now
    )


def past_first_stop(plan: Plan, position: Position, policy: ProgressPolicy) -> bool:
    """Whether the fix lies on the path after the first stop's departure trigger."""

    first = plan.zones[0].departure_m if plan.zones else None
    after = plan.path.call_distances_m[0] if first is None else first
    found = candidates(plan.path, position.lon, position.lat, _sigma(policy), policy.reach_sigmas)
    return any(candidate.along_m > after for candidate in found)


def _first_floor(beam: tuple[Hypothesis, ...]) -> float:
    """Where commits start for a new track: nothing before the first fix was observed."""

    return min(h.along_m for h in beam)


# --- extending hypotheses ----------------------------------------------------------------------


def _search_window(
    live: tuple[Hypothesis, ...], at_us: int, speed: float, policy: ProgressPolicy
) -> tuple[float, float] | None:
    """The stretch of path any live hypothesis could have reached since its last fix; the whole
    path when there is none."""

    if not live:
        return None
    elapsed = max((at_us - h.at_us) / 1_000_000 for h in live)
    reach = policy.reach_sigmas * math.hypot(policy.gps_sigma_m, policy.chord_min_sigma_m)
    return (
        min(h.along_m for h in live) - policy.jitter_m,
        max(h.along_m for h in live) + speed * max(0.0, elapsed) + reach,
    )


def _start(
    plan: Plan,
    found: list[Candidate],
    position: Position,
    at: Instant,
    at_us: int,
    policy: ProgressPolicy,
) -> list[Hypothesis]:
    """The first fix: one hypothesis per candidate, with a prior on absolute lateness that is
    broad for running late and narrow for running early."""

    out: list[Hypothesis] = []
    for candidate in found:
        lateness = plan.timetable.lateness_us(at_us, candidate.along_m)
        sigma = policy.start_early_sigma_s if lateness < 0 else policy.start_lateness_sigma_s
        prior = -0.5 * (lateness / sigma) ** 2
        fit = _emission(candidate, position, plan.path, policy)
        history = (Placement(candidate.along_m, at),)
        out.append(Hypothesis(candidate.along_m, at, lateness, fit + prior, history, at_us))
    return out


def _extend(
    plan: Plan,
    live: tuple[Hypothesis, ...],
    found: list[Candidate],
    position: Position,
    at: Instant,
    at_us: int,
    speed: float,
    moved: float | None,
    policy: ProgressPolicy,
) -> list[Hypothesis]:
    """Each candidate continues the live hypothesis that reaches it most plausibly (Viterbi)."""

    timetable = plan.timetable
    jitter = policy.jitter_m
    # On a real shape a reading must keep up with how far the fixes moved apart.
    shortfall = moved - policy.move_slack_m if moved is not None and plan.path.shaped else None
    move_sigma = policy.move_sigma_m
    late: dict[float, float] = {}  # lateness at `at` per place, shared by the pairs
    out: list[Hypothesis] = []
    for candidate in found:
        fit = _emission(candidate, position, plan.path, policy)
        best: tuple[float, Hypothesis, float, float] | None = None  # score, from, along, late
        for h in live:
            # Backwards beyond GPS noise is impossible; a reading advanced by the distance
            # driven may have overshot a road shorter than the one it estimated.
            if candidate.along_m < h.along_m - jitter - policy.reckon_slack * h.reckoned_m:
                continue
            # A small step back is no movement, except to correct an overshoot.
            along = candidate.along_m if h.reckoned_m > 0 else max(candidate.along_m, h.along_m)
            lateness = late.get(along)
            if lateness is None:
                lateness = late[along] = timetable.lateness_us(at_us, along)
            move = _transition(h, along, at_us, lateness, speed, policy)
            if move is None:
                continue
            if shortfall is not None and h.off_path_since is None:  # placed by the last fix
                short = shortfall - (along - h.along_m)
                if short > 0:
                    move -= 0.5 * (short / move_sigma) ** 2
            score = h.log_p + move + fit
            if best is None or score > best[0]:
                best = (score, h, along, lateness)
        if best is not None:
            score, h, along, lateness = best
            history = _placed(h.history, along, at)
            out.append(Hypothesis(along, at, lateness, score, history, at_us))
    return out


def _placed(history: tuple[Placement, ...], along_m: float, at: Instant) -> tuple[Placement, ...]:
    """`history` with a fix at `along_m`. Of a run of fixes at one place only the first and the
    last are kept: a crossing is bracketed by the last fix before it and the first fix past it,
    so the fixes in between are never read, and a long dwell stays two placements."""

    placement = Placement(along_m, at)
    if len(history) >= 2 and history[-1].along_m == along_m == history[-2].along_m:
        return (*history[:-1], placement)
    return (*history, placement)


def _held(
    plan: Plan,
    live: tuple[Hypothesis, ...],
    at: Instant,
    at_us: int,
    moved: float | None,
    here: tuple[float, float],
    policy: ProgressPolicy,
) -> list[Hypothesis]:
    """A fix the geometry does not explain (a detour, a shape that misses the road taken, a road
    far from a stop-to-stop chord) says nothing about where along the path the vehicle is: each
    hypothesis may also hold, at a fixed cost, instead of dying for it.

    On a real shape it advances by the distance the fixes moved, never into the next call's
    arrival trigger: a vehicle on another road of about the same length keeps its lateness, one
    on a longer detour grows late once it reaches that limit, and since the road left to the
    next call is never shorter than the straight line to it, a fix off to the side or behind
    adds nothing. Its place stays out of the history: a departure it passes is committed once
    a fix on the path brackets it. On a stop-to-stop chord, which no road follows, it stays
    where it was; the lateness comes from the leg estimate (`_chord_estimate`)."""

    out: list[Hypothesis] = []
    for h in live:
        along, lateness = h.along_m, h.lateness_s
        if plan.path.shaped and moved is not None:
            trigger, call = _next_arrival(plan, h.along_m)
            at_call = plan.path.call_distances_m[call]
            left = distance_m(here, point_at(plan.path, at_call))
            along = max(h.along_m, min(h.along_m + moved, trigger - 1.0, at_call - left))
            lateness = plan.timetable.lateness_us(at_us, along)
        # It keeps the time of its last fix on the path: coming back onto the path far ahead
        # (a detour past the next stop) stays within the speed limit counted from there.
        out.append(
            Hypothesis(
                along,
                h.at,
                lateness,
                h.log_p + policy.off_path_log_p,
                h.history,
                h.at_us,
                h.off_path_since or at,
                h.reckoned_m + (along - h.along_m),
            )
        )
    return out


def _chord_estimate(
    plan: Plan,
    best: Hypothesis,
    previous: OffPath | None,
    here: tuple[float, float],
    at: Instant,
    at_us: int,
    speed: float,
) -> OffPath | None:
    """While the best reading holds off a stop-to-stop chord: where the fix puts the vehicle on
    its leg (`_leg_share`), never behind the held reading or an earlier estimate, never faster
    than the mode allows, never past the next call's arrival trigger. Lateness only."""

    if plan.path.shaped or best.off_path_since is None or best.at >= at:
        return None
    floor = best.along_m
    if previous is not None and previous.along_m >= floor:
        floor = previous.along_m
    trigger, _ = _next_arrival(plan, best.along_m)
    ceiling = min(trigger, best.along_m + speed * ((at_us - best.at_us) / 1_000_000))
    along = min(max(_leg_share(plan, best.along_m, here), floor), max(floor, ceiling))
    return OffPath(along, at, plan.timetable.lateness_s(at, along))


def _leg_share(plan: Plan, along_m: float, here: tuple[float, float]) -> float:
    """Where a fix puts the vehicle on its leg: between the last call left and the next one by
    its share of distance to the two. Unlike a projection onto the chord, it keeps growing on a
    road that runs sideways."""

    calls = plan.path.call_distances_m
    last = max(plan.call_index_at(along_m), 0)
    if last + 1 >= len(calls):
        return along_m
    start, end = calls[last], calls[last + 1]
    to_last = distance_m(here, point_at(plan.path, start))
    to_next = distance_m(here, point_at(plan.path, end))
    share = 1.0 if to_last + to_next <= 0 else to_last / (to_last + to_next)
    return start + share * (end - start)


def _next_arrival(plan: Plan, along_m: float) -> tuple[float, int]:
    """The first arrival trigger ahead of `along_m` and its call; past the last, the end."""

    for trigger in plan.triggers_between(along_m, math.inf):
        if trigger.kind == "arrival":
            return trigger.along_m, trigger.call
    last = len(plan.path.call_distances_m) - 1
    return plan.path.call_distances_m[last] + 1.0, last


def _prune(extended: list[Hypothesis], policy: ProgressPolicy) -> tuple[Hypothesis, ...]:
    """Most likely first, scores relative to it; near-identical positions merged; at most
    `beam`, none more than `prune` behind."""

    ordered = sorted(extended, key=lambda h: (-h.log_p, h.along_m, h.off_path_since is not None))
    top = ordered[0].log_p
    kept: list[Hypothesis] = []
    for h in ordered:
        if top - h.log_p > policy.prune or len(kept) >= policy.beam:
            break
        if any(abs(h.along_m - k.along_m) < 1.0 for k in kept):
            continue
        kept.append(h.evolve(log_p=h.log_p - top))
    return tuple(kept)


def _unmatched(track: Track | None, at: Instant, policy: ProgressPolicy) -> Step:
    """A fix before any hypothesis exists that matches no segment: nothing to track yet."""

    if track is None:
        track = Track((), 0.0)
    since = track.unmatched_since or at
    held = at - since >= timedelta(seconds=policy.off_route_hold_s)
    return Step(replace(track, unmatched_since=since, seen_at=at), None, (), since, held)


# --- scoring -----------------------------------------------------------------------------------


def _sigma(policy: ProgressPolicy) -> Callable[[Path, float], float]:
    """How far a fix may lie from a segment (one standard deviation): GPS error combined with
    the trust of the geometry there."""

    def sigma(path: Path, segment_m: float) -> float:
        if path.shaped:
            geometry = policy.shape_sigma_m
        else:
            chord = max(policy.chord_min_sigma_m, policy.chord_k * segment_m)
            geometry = min(policy.chord_max_sigma_m, chord)
        return math.hypot(policy.gps_sigma_m, geometry)

    return sigma


def _emission(
    candidate: Candidate, position: Position, path: Path, policy: ProgressPolicy
) -> float:
    """Log-likelihood of the fix at the candidate: lateral fit, and heading if it has one."""

    score = -0.5 * (candidate.lateral_m / candidate.sigma_m) ** 2 - math.log(candidate.sigma_m)
    if position.bearing is not None:
        turn = abs((position.bearing - candidate.heading_deg + 180.0) % 360.0 - 180.0)
        spread = policy.bearing_sigma_deg if path.shaped else policy.chord_bearing_sigma_deg
        score -= 0.5 * (turn / spread) ** 2
    return score


def _transition(
    h: Hypothesis,
    along: float,
    at_us: int,
    lateness_s: float,
    speed: float,
    policy: ProgressPolicy,
) -> float | None:
    """Log-likelihood of moving from `h` to `along` at `at_us`; None if impossible.

    The speed limit allows only GPS jitter as slack, never the geometry's uncertainty: a loose
    chord widens where a fix may lie, not how far the vehicle may travel."""

    moved = along - h.along_m
    elapsed = (at_us - h.at_us) / 1_000_000
    if elapsed <= 0:
        return None
    if max(0.0, moved) - policy.jitter_m > speed * elapsed:
        return None
    change = lateness_s - h.lateness_s
    if change >= 0:
        spread = policy.loss_sigma_base_s + policy.loss_sigma_rate * elapsed
    else:
        spread = policy.gain_sigma_base_s + policy.gain_sigma_rate * elapsed
    return -0.5 * (change / spread) ** 2
