"""Every corpus journey gives its pinned output-level compare stream
(`tests/realtime/corpus_stream.py`): the record a port of the core is compared on.

A deliberate change of the core or the writer changes them: review
`python -m tests.realtime.corpus_stream check`, then rewrite with `... write`.
"""

from __future__ import annotations

import gzip

import pytest

from tests.realtime.corpus import names
from tests.realtime.corpus_stream import path, stream


@pytest.mark.parametrize("name", names())
def test_a_real_journey_gives_its_pinned_stream(name: str) -> None:
    stored = path(name)
    assert stored.is_file(), (
        f"no stream for {name}: run python -m tests.realtime.corpus_stream write"
    )
    with gzip.open(stored, "rt", encoding="utf-8", newline="\n") as pinned:
        assert "".join(stream(name)) == pinned.read()
