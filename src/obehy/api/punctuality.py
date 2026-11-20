"""How late something is as a rider sees it, decided once for every answer (boards, trips,
vehicles, history) so no client keeps its own band.

A delay is in shown minutes: the expected time cut to the minute less the scheduled one cut to
the minute, as the two times read on a board. A delay known only in seconds (a vehicle's, a
day's in history) is the same against a whole-minute schedule: its floor in minutes. Its band
comes from the API limits (`[punctuality]`): early, on time, late, very late; a cancelled call
is cancelled; a time with no live estimate, or one gone stale, has no band (`unknown`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Literal

from obehy.api.limits import Limits, load_limits

Punctuality = Literal["early", "on_time", "late", "very_late", "unknown", "cancelled"]

# Call statuses whose estimate is live realtime (`CallStatus`).
LIVE = frozenset({"predicted", "actual", "inferred"})


@dataclass(frozen=True, slots=True)
class Shown:
    """A delay as shown: in whole minutes, and its band."""

    delay_min: int | None
    punctuality: Punctuality


UNKNOWN = Shown(None, "unknown")
CANCELLED = Shown(None, "cancelled")


@cache
def _limits() -> Limits:
    return load_limits()


def band(delay_min: int, limits: Limits | None = None) -> Punctuality:
    """The band of a delay in shown minutes."""

    found = limits or _limits()
    if delay_min <= found.early_max_min:
        return "early"
    if delay_min <= found.on_time_max_min:
        return "on_time"
    if delay_min <= found.late_max_min:
        return "late"
    return "very_late"


def minutes(scheduled: datetime, expected: datetime) -> int:
    """Whole minutes between the two times as shown (each cut to its minute)."""

    return math.floor(expected.timestamp() / 60) - math.floor(scheduled.timestamp() / 60)


def of_seconds(delay_s: int | None, *, stale: bool = False) -> Shown:
    """A delay known in seconds, as shown against a whole-minute schedule."""

    if delay_s is None:
        return UNKNOWN
    delay_min = math.floor(delay_s / 60)
    return Shown(delay_min, "unknown" if stale else band(delay_min))


def of_time(
    scheduled: datetime | None,
    estimated: datetime | None,
    status: str,
    *,
    stale: bool = False,
) -> Shown:
    """A call's time: cancelled; no band without a live estimate; a stale one keeps its
    minutes but loses its band."""

    if status == "cancelled":
        return CANCELLED
    if scheduled is None or estimated is None or status not in LIVE:
        return UNKNOWN
    delay_min = minutes(scheduled, estimated)
    return Shown(delay_min, "unknown" if stale else band(delay_min))


def rider_time(
    arrival: tuple[datetime | None, datetime | None],
    departure: tuple[datetime | None, datetime | None],
    *,
    last: bool,
) -> tuple[datetime | None, datetime | None]:
    """The (scheduled, estimated) time a rider meets at a call: its departure, at the trip's
    last call its arrival; the other one where that has no scheduled time. An estimate missing
    on that side is the other side's."""

    first, second = (arrival, departure) if last else (departure, arrival)
    scheduled = first[0] if first[0] is not None else second[0]
    side = first if first[0] is not None else second
    other = second if side is first else first
    return scheduled, side[1] if side[1] is not None else other[1]
