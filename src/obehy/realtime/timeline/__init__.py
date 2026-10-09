"""The timeline engine: progress, events, delay and estimates of one journey instance."""

from __future__ import annotations

from dataclasses import replace

from obehy.realtime.index import IndexView, Trip
from obehy.realtime.model import Delay, Effect, Instance, Observation
from obehy.realtime.policy import Policy


def advance(
    instance: Instance, trip: Trip, observation: Observation, index: IndexView, policy: Policy
) -> tuple[Instance, list[Effect]]:
    """Apply one bound observation to its instance."""

    del trip, index
    delay = observation.first(Delay)
    delay_s = instance.delay_s
    if delay is not None and delay.seconds > policy.delay_discard_below_s:
        delay_s = delay.seconds
    return replace(instance, updated_at=observation.at, delay_s=delay_s), []
