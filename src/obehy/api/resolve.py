"""Which trip and journey a request means, decided once (BASE_PLAN.md sections 6 and 30).

A trip is its feed trip ID and service date; its journeys are the keys it has on the date (the
realtime policy's `[journey]` namespaces first, the one history and realtime show it by). A
source key names the one trip (or one rail run's parts) running on the date, or is ambiguous.
A trip ID of an older timetable version is gone: history knows the journey it ran as.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from fastapi import HTTPException

from obehy.api import schema
from obehy.api.db import Snapshot
from obehy.realtime_config import policy_values

# A trip's journey key: the first of these it has (realtime policy `[journey]`).
JOURNEY_NAMESPACES = list(policy_values().journey_namespaces)


@dataclass(frozen=True, slots=True)
class TripOnDate:
    """A trip of the active release on a service date."""

    trip_id: str
    service_date: date
    feed: str
    runs: bool
    # Every key it has on the date, the journey namespaces first in preference order.
    keys: tuple[schema.JourneyKey, ...]

    @property
    def journey(self) -> schema.JourneyKey | None:
        """The key its journey is known by, if it has one in a journey namespace."""

        first = self.keys[0] if self.keys else None
        return first if first is not None and first.namespace in JOURNEY_NAMESPACES else None

    def first(self, namespace: str) -> str | None:
        """Its first key in `namespace`."""

        return next((k.key for k in self.keys if k.namespace == namespace), None)


def trip_on(snap: Snapshot, trip_id: str, service_date: date) -> TripOnDate | None:
    """The trip of the active release on the date, or None when the release has no such trip."""

    row = snap.one(
        "trip_on_date",
        {"trip_id": trip_id, "service_date": service_date, "namespaces": JOURNEY_NAMESPACES},
    )
    if row is None:
        return None
    return TripOnDate(
        trip_id=trip_id,
        service_date=service_date,
        feed=row["feed"],
        runs=row["runs"],
        keys=tuple(schema.JourneyKey.model_validate(k) for k in row["keys"]),
    )


def running(snap: Snapshot, trip_id: str, service_date: date) -> TripOnDate:
    """The trip, running on the date; 404 otherwise."""

    trip = trip_on(snap, trip_id, service_date)
    if trip is None:
        raise HTTPException(404, f"no trip {trip_id}")
    if not trip.runs:
        raise HTTPException(404, f"{trip_id} does not run on {service_date}")
    return trip


def gone(snap: Snapshot, trip_id: str, service_date: date) -> schema.JourneyKey | None:
    """The journey a trip ID the active release no longer has ran as on the date."""

    row = snap.one("trip_gone", {"trip_id": trip_id, "service_date": service_date})
    return None if row is None else schema.JourneyKey.model_validate(row)


@dataclass(frozen=True, slots=True)
class ByKey:
    """What a source key names on a date: one trip, or why not."""

    outcome: Literal["resolved", "not_found", "ambiguous"]
    trip_id: str | None = None
    detail: str | None = None


def by_key(snap: Snapshot, namespace: str, key: str, service_date: date) -> ByKey:
    """The one trip a source key names on the date: several only when they are one rail run's
    parts (its first then)."""

    rows = snap.all(
        "trip_by_key", {"namespace": namespace, "key": key, "service_date": service_date}
    )
    if not rows:
        return ByKey("not_found", detail=f"no trip {namespace} {key} runs on {service_date}")
    runs = {r["run_key"] for r in rows}
    if len(rows) > 1 and (len(runs) > 1 or None in runs):
        names = ", ".join(r["trip_id"] for r in rows)
        return ByKey(
            "ambiguous", detail=f"{namespace} {key} names several trips on {service_date}: {names}"
        )
    return ByKey("resolved", trip_id=rows[0]["trip_id"])


def recorded_journey(
    snap: Snapshot, trip_id: str, since: date, until: date
) -> schema.JourneyKey | None:
    """The journey a trip ID's runs between the dates are recorded under: of its keys, the one
    history recorded on most days (each source records by its own key), else the preferred;
    for a trip ID the release no longer has, the key history recorded it under."""

    row = snap.one(
        "trip_journey_key",
        {"trip_id": trip_id, "since": since, "until": until, "namespaces": JOURNEY_NAMESPACES},
    )
    return None if row is None else schema.JourneyKey(namespace=row["namespace"], key=row["key"])
