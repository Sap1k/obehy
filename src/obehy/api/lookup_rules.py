"""Which lazy details (compositions) can be asked for a trip: each lookup's manifest declares
the trips and days it answers for (`applies`), so the API says so without asking upstream and
the web asks only where an answer can come."""

from __future__ import annotations

from functools import cache

from obehy.api.common import today
from obehy.api.db import Snapshot
from obehy.api.resolve import TripOnDate
from obehy.realtime_config import Lookup, declared_lookups


@cache
def _lookups(kind: str) -> tuple[Lookup, ...]:
    return tuple(lookup for lookup in declared_lookups() if lookup.kind == kind)


def can_ask(snap: Snapshot, kind: str, trip: TripOnDate) -> bool:
    """Whether a lookup of `kind` answers for the trip on its date."""

    lookups = _lookups(kind)
    if not lookups:
        return False
    keys = {k.namespace: k.key for k in reversed(trip.keys)}  # the first of each namespace
    on = today(snap)
    return any(
        trip.feed in lookup.feeds and lookup.applies.refusal(keys, trip.service_date, on) is None
        for lookup in lookups
    )
