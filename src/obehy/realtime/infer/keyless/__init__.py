"""Keyless inference: a vehicle without a trip key bound by what it shows (BASE_PLAN.md section
19.2; the first source is Arriva Express, docs/sources/arriva-express.md).

Candidates come from the line and are hard-filtered by destination and time (`candidates.py`).
The episode's departures, else the current fix, give each a time residual (`score.py`). The best
candidate binds only if its residual is at most `accept_s` (`gps_accept_s` for a fix alone) and
the runner-up's is at least `margin_s` larger; otherwise the observation is `ambiguous`. Kept
apart from keyed binding, with its own policy (`[keyless]`); the core calls it only for an
observation with a `LineRef` and no `TripKey`.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass, replace
from typing import Literal

from obehy.realtime.index import IndexView
from obehy.realtime.infer.keyed import Match, in_window, span
from obehy.realtime.infer.keyless.candidates import candidates
from obehy.realtime.infer.keyless.score import bound_contradicts, place, position_residual
from obehy.realtime.model import (
    Binding,
    JourneyKey,
    KeylessEpisode,
    LineRef,
    Observation,
    Reason,
    VehicleState,
)
from obehy.realtime.policy import Policy
from obehy.realtime.timeline.plan import PlanCache
from obehy.realtime.times import Instant

Evidence = Literal["departures", "position"]


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome and its explanation: how many candidates passed the hard filters, the
    evidence used, and the best and runner-up residuals (seconds)."""

    result: Match | Reason
    candidates: int = 0
    evidence: Evidence | None = None
    best_s: float | None = None
    runner_up_s: float | None = None


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def bind(
    line: LineRef,
    episode: KeylessEpisode,
    observation: Observation,
    index: IndexView,
    policy: Policy,
    plans: PlanCache,
) -> Decision:
    found = candidates(line, episode.destination, observation.at, index, policy.keyless)
    if isinstance(found, Reason):
        return Decision(found)
    by_departures: list[tuple[float, Match]] = []
    by_position: list[tuple[float, Match]] = []
    survivors = 0
    for match in found:
        placed = place(match, episode, index, policy)
        if placed is None:
            continue  # its calls contradict the episode
        survivors += 1
        timed = [r for r in placed.residuals if r is not None]
        if timed:
            by_departures.append((_median(timed), match))
            continue
        residual = position_residual(match, placed, observation, index, policy, plans)
        if residual is not None:
            by_position.append((residual, match))
    if not survivors:
        return Decision(Reason.NOT_IN_SERVICE, len(found))
    keyless = policy.keyless
    if by_departures:
        scored, accept, evidence = by_departures, keyless.accept_s, "departures"
    elif by_position:
        scored, accept, evidence = by_position, keyless.gps_accept_s, "position"
    else:
        return Decision(Reason.AMBIGUOUS, survivors)  # nothing to tell them apart yet
    scored.sort(key=lambda item: (item[0], item[1].journey))
    best, match = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else None
    decision = Decision(Reason.AMBIGUOUS, survivors, evidence, best, runner_up)
    if best > accept or (runner_up is not None and runner_up - best < keyless.margin_s):
        return decision
    return Decision(match, survivors, evidence, best, runner_up)


def continued(
    previous: VehicleState | None,
    episode: KeylessEpisode,
    at: Instant,
    instances: Container[JourneyKey],
    index: IndexView,
    policy: Policy,
) -> Binding | None:
    """An inferred binding continues while its episode does, the time stays in the journey's
    window and the episode does not contradict the trip (section 19.5); otherwise None, and the
    vehicle is inferred again."""

    if previous is None or previous.binding is None or previous.binding.method != "inferred":
        return None
    if previous.episode is None or previous.episode.started_at != episode.started_at:
        return None  # line or destination changed, or the vehicle was gone too long
    binding = previous.binding
    if binding.journey not in instances:
        return None
    trip = index.trip(binding.trip_id)
    match = Match(binding.journey, trip, *span(trip, binding.journey))
    if not in_window(match, at, policy):
        return None
    contradicted, _ = bound_contradicts(match, episode, index, policy)
    return None if contradicted else binding


def confirm(binding: Binding, episode: KeylessEpisode, index: IndexView, policy: Policy) -> Binding:
    """An inferred binding is confirmed once `confirm_departures` of its episode's departures
    agree with the trip."""

    if binding.confirmed:
        return binding
    trip = index.trip(binding.trip_id)
    match = Match(binding.journey, trip, *span(trip, binding.journey))
    _, agreeing = bound_contradicts(match, episode, index, policy)
    if agreeing >= policy.keyless.confirm_departures:
        return replace(binding, confirmed=True)
    return binding
