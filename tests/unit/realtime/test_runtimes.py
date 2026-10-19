"""Hop running times (`timeline.runtimes`): recency weights, the timetable prior, clipping."""

from __future__ import annotations

import math
from collections import deque

from obehy.realtime.model import HopKey, Traversal
from obehy.realtime.policy import load_policy
from obehy.realtime.timeline.runtimes import KEEP_DECAYS, forget, ratio
from tests.realtime.observations import local

POLICY = load_policy().prediction
HOP: HopKey = ("bus", "jdf:A", "jdf:B", False)
NOW = local("2026-10-08 08:00")


def _hops(*traversals: tuple[str, float, float]) -> dict[HopKey, deque[Traversal]]:
    return {
        HOP: deque((local(f"2026-10-08 {at}"), taken, planned) for at, taken, planned in traversals)
    }


def test_a_hop_no_one_drove_has_no_ratio() -> None:
    assert ratio({}, HOP, NOW, POLICY) is None


def test_one_traversal_moves_the_ratio_only_part_of_the_way() -> None:
    value = ratio(_hops(("08:00", 120.0, 60.0)), HOP, NOW, POLICY)
    prior = POLICY.runtime_prior_weight
    assert value == (prior + 2.0) / (prior + 1.0)


def test_older_traversals_weigh_less() -> None:
    value = ratio(_hops(("07:30", 120.0, 60.0), ("08:00", 60.0, 60.0)), HOP, NOW, POLICY)
    old = math.exp(-1800 / POLICY.runtime_decay_s)
    prior = POLICY.runtime_prior_weight
    assert value is not None and abs(value - (prior + old * 2 + 1) / (prior + old + 1)) < 1e-9


def test_a_wild_traversal_is_clipped_and_a_future_one_ignored() -> None:
    hops = _hops(("08:00", 6000.0, 60.0), ("08:05", 60.0, 60.0))
    prior, most = POLICY.runtime_prior_weight, POLICY.runtime_ratio_max
    assert ratio(hops, HOP, NOW, POLICY) == (prior + most) / (prior + 1)


def test_hops_not_driven_for_a_while_are_forgotten() -> None:
    hops = _hops(("06:00", 60.0, 60.0))
    forget(hops, NOW, POLICY)
    assert hops == {} and KEEP_DECAYS * POLICY.runtime_decay_s < 7200
