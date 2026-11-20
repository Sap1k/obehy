"""Every journey of the real-trace corpus (`tests/realtime/corpus.py`) through the core."""

from __future__ import annotations

import pytest

from tests.realtime.corpus import names, run

# Known failures of the current core, each with what goes wrong.
KNOWN = {
    # An SŽ map point time (a whole minute, SZ-Q12) lands 5 s before the fix crossing of the
    # call before it.
    "sz-mapa-rail-long-Tr-3246-KADR000957-00-2026-TR-3246-KADR0009": "SŽ minute vs crossing",
}


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, marks=pytest.mark.xfail(reason=KNOWN[n], strict=True)) if n in KNOWN else n
        for n in names()
    ],
)
def test_a_real_journey_runs_and_its_events_follow_call_order(name: str) -> None:
    journey = run(name)
    for instance in journey.state.instances.values():
        times = [
            t
            for call in instance.calls
            for t in (call.passed_arrival, call.passed_departure)
            if t is not None
        ]
        assert times == sorted(times), instance.journey
