"""Every real-trace corpus journey gives the core's recorded outputs (`tests/realtime/golden.py`).

A deliberate change of the core changes them: review `python -m tests.realtime.golden diff`,
then rewrite them with `python -m tests.realtime.golden write`.
"""

from __future__ import annotations

import pytest

from tests.realtime.corpus import names
from tests.realtime.golden import dump, golden, path


@pytest.mark.parametrize("name", names())
def test_a_real_journey_gives_its_golden_outputs(name: str) -> None:
    stored = path(name)
    assert stored.is_file(), f"no golden for {name}: run python -m tests.realtime.golden write"
    assert dump(golden(name)) == stored.read_text(encoding="utf-8")
