"""Which source vehicle IDs are physical vehicles (oběhy follow only those): each source's
manifest says so per mode (`[vehicles] physical`). A metro run or a train keyed by its trip or
number names no vehicle that works a day."""

from __future__ import annotations

from functools import cache
from typing import Any

from obehy.realtime.manifest import Source, physical_vehicles


@cache
def _sources() -> dict[str, Source]:
    return physical_vehicles()


def is_physical(source: str, mode: str | None) -> bool:
    """Whether `source`'s vehicle IDs on `mode`'s journeys (None: unknown) are physical."""

    found = _sources().get(source)
    return True if found is None else found.physical(mode)


@cache
def physical_table() -> dict[str, list[Any]]:
    """The policy as SQL parameters: the `physical` table of the circulation queries, one row
    per source and mode (`default` for a source's other modes)."""

    rows = [
        (source, mode, physical)
        for source, manifest in sorted(_sources().items())
        for mode, physical in sorted(manifest.physical_vehicles.items())
    ]
    return {
        "physical_sources": [r[0] for r in rows],
        "physical_modes": [r[1] for r in rows],
        "physical": [r[2] for r in rows],
    }
