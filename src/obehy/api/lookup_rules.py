"""Which lazy details (compositions) can be asked for a trip: each lookup's manifest declares
the trips and days it answers for (`applies`), so the API says so without asking upstream and
the web asks only where an answer can come."""

from __future__ import annotations

from datetime import date
from functools import cache

from obehy.api.db import Snapshot
from obehy.realtime.manifest import Lookup, declared_lookups
from obehy.realtime.times import local_date


@cache
def _lookups(kind: str) -> tuple[Lookup, ...]:
    return tuple(lookup for lookup in declared_lookups() if lookup.kind == kind)


def can_ask(snap: Snapshot, kind: str, trip_id: str, service_date: date) -> bool:
    """Whether a lookup of `kind` answers for the trip on the date."""

    lookups = _lookups(kind)
    if not lookups:
        return False
    namespaces = sorted({ns for lookup in lookups for ns in lookup.applies.namespaces})
    row = snap.one(
        "trip_lookup_keys",
        {"trip_id": trip_id, "service_date": service_date, "namespaces": namespaces},
    )
    if row is None:
        return False
    today = local_date(snap.now)
    return any(
        row["feed"] in lookup.feeds
        and lookup.applies.refusal(row["keys"], service_date, today) is None
        for lookup in lookups
    )
