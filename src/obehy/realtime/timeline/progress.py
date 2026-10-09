"""Progress along a trip's path as map matching over a beam of hypotheses.

`BASE_PLAN.md` section 20.4 and `docs/R1_SLICE.md` section 9. A fix is never matched on its own:
each fix extends every live hypothesis to each place on the path it may be (one candidate per
segment), scored by three terms:

- geometry fit: lateral distance against the trust of the path there (a real shape closely, a
  stop-to-stop chord less, the longer the less), plus the heading when the fix has a bearing;
- forward speed: no backward move beyond GPS jitter, no move faster than the mode allows;
- change of lateness: time lost or gained against the timetable between the two fixes, with a
  spread growing with the time between them. Absolute lateness is never penalised.

A fix the path does not explain holds a hypothesis as if the fix were missing, at a fixed cost.
The most likely hypothesis is the live position; crossings are committed once the readings
agree (`commit.py`). GPS fixes are the only evidence in R1; source-reported progress (SŽ
passages, a next stop) will be scored against the same hypotheses as a further term.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta

from obehy.realtime.model import Hypothesis, Placement, Position, Progress, Track
from obehy.realtime.policy import ProgressPolicy
from obehy.realtime.timeline.commit import Crossing, commit
from obehy.realtime.timeline.path import Candidate, Path, candidates
from obehy.realtime.timeline.plan import Plan
from obehy.realtime.times import Instant


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
    policy: ProgressPolicy,
) -> Step:
    """Extend the journey's hypotheses with one fix and commit what they agree on."""

    if len(plan.path.points) < 2:
        return Step(track or Track((), 0.0), None, (), None, False)
    live = track.hypotheses if track is not None else ()
    speed = policy.max_speed_mps(mode)
    window = _search_window(live, at, speed, policy)
    found = candidates(
        plan.path, position.lon, position.lat, _sigma(policy), policy.reach_sigmas, window
    )
    if live:
        extended = _extend(plan, live, found, position, at, speed, policy)
        extended += _held(live, at, policy)
    else:
        extended = _start(plan, found, position, at, policy)
    if not extended:
        return _unmatched(track, at, policy)

    beam = _prune(extended, policy)
    committed_m = track.committed_m if live and track is not None else _first_floor(beam)
    crossed_at = track.crossed_at if track is not None else None
    done = commit(plan, beam, committed_m, crossed_at, at, policy)
    best = done.beam[0]
    progress = Progress(best.along_m, plan.call_index_at(best.along_m), at)
    since = best.off_path_since
    off_route = since is not None and at - since >= timedelta(seconds=policy.off_route_hold_s)
    new_track = Track(done.beam, done.committed_m, seen_at=at, crossed_at=done.crossed_at)
    return Step(new_track, progress, done.crossed, since, off_route)


def _first_floor(beam: tuple[Hypothesis, ...]) -> float:
    """Where commits start for a new track: nothing before the first fix was observed."""

    return min(h.along_m for h in beam)


# --- extending hypotheses ----------------------------------------------------------------------


def _search_window(
    live: tuple[Hypothesis, ...], at: Instant, speed: float, policy: ProgressPolicy
) -> tuple[float, float] | None:
    """The stretch of path any live hypothesis could have reached since its last fix; the whole
    path when there is none."""

    if not live:
        return None
    elapsed = max((at - h.at).total_seconds() for h in live)
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
    policy: ProgressPolicy,
) -> list[Hypothesis]:
    """The first fix: one hypothesis per candidate, with a broad prior on absolute lateness."""

    out: list[Hypothesis] = []
    for candidate in found:
        lateness = plan.timetable.lateness_s(at, candidate.along_m)
        prior = -0.5 * (lateness / policy.start_lateness_sigma_s) ** 2
        fit = _emission(candidate, position, plan.path, policy)
        history = (Placement(candidate.along_m, at),)
        out.append(Hypothesis(candidate.along_m, at, lateness, fit + prior, history))
    return out


def _extend(
    plan: Plan,
    live: tuple[Hypothesis, ...],
    found: list[Candidate],
    position: Position,
    at: Instant,
    speed: float,
    policy: ProgressPolicy,
) -> list[Hypothesis]:
    """Each candidate continues the live hypothesis that reaches it most plausibly (Viterbi)."""

    out: list[Hypothesis] = []
    for candidate in found:
        fit = _emission(candidate, position, plan.path, policy)
        best: Hypothesis | None = None
        for h in live:
            if candidate.along_m < h.along_m - policy.jitter_m:
                continue  # backwards beyond GPS noise: impossible
            along = max(candidate.along_m, h.along_m)  # a small step back is no movement
            lateness = plan.timetable.lateness_s(at, along)
            move = _transition(h, along, at, lateness, speed, policy)
            if move is None:
                continue
            score = h.log_p + move + fit
            if best is None or score > best.log_p:
                history = (*h.history, Placement(along, at))
                best = Hypothesis(along, at, lateness, score, history)
        if best is not None:
            out.append(best)
    return out


def _held(live: tuple[Hypothesis, ...], at: Instant, policy: ProgressPolicy) -> list[Hypothesis]:
    """A fix the geometry does not explain (a detour, a road far from a stop-to-stop chord)
    says nothing about where the vehicle is: each hypothesis may also hold as if the fix were
    missing, at a fixed cost, instead of dying for it."""

    return [
        h.evolve(log_p=h.log_p + policy.off_path_log_p, off_path_since=h.off_path_since or at)
        for h in live
    ]


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
    at: Instant,
    lateness_s: float,
    speed: float,
    policy: ProgressPolicy,
) -> float | None:
    """Log-likelihood of moving from `h` to `along` at `at`; None if impossible.

    The speed limit allows only GPS jitter as slack, never the geometry's uncertainty: a loose
    chord widens where a fix may lie, not how far the vehicle may travel."""

    moved = along - h.along_m
    elapsed = (at - h.at).total_seconds()
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
