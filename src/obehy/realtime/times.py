"""The only module that converts realtime times (BASE_PLAN.md section 19.3).

`Instant` is an aware UTC datetime. `ServiceTime` is a schedule time: wall-clock seconds from
local midnight of the service date, exactly as JrUtil writes it (values past 86400 continue into
the next day). Reading a wall-clock time on a DST night is approximate by design: the repeated
autumn hour takes its first occurrence and the skipped spring hour is read with the pre-change
offset. Source local times are stricter: a time that does not exist is rejected.

Arithmetic happens on UTC instants only. Python adds a `timedelta` to an aware non-UTC datetime
in wall-clock time, which silently breaks durations across a DST change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import NewType
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")

Instant = NewType("Instant", datetime)


class TimeError(ValueError):
    """A value that cannot be a valid time in its declared meaning."""


def instant(value: datetime) -> Instant:
    """An aware datetime as a UTC `Instant`; naive input is a programming error."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise TimeError(f"naive datetime {value.isoformat()} cannot be an Instant")
    return Instant(value.astimezone(UTC))


@dataclass(frozen=True, slots=True, order=True)
class ServiceTime:
    service_date: date
    seconds: int

    def instant(self) -> Instant:
        """The instant of this wall-clock schedule time (first occurrence when repeated)."""

        wall = datetime.combine(self.service_date, time()) + timedelta(seconds=self.seconds)
        return instant(wall.replace(tzinfo=PRAGUE, fold=0))


def local_date(value: Instant) -> date:
    """The Europe/Prague calendar date of an instant."""

    return value.astimezone(PRAGUE).date()


def candidate_service_dates(value: Instant) -> tuple[date, date, date]:
    """Service dates an observation can belong to: local yesterday, today and tomorrow."""

    today = local_date(value)
    return today - timedelta(days=1), today, today + timedelta(days=1)


def _readings(wall: datetime) -> list[Instant]:
    """Every instant that reads as the naive local time `wall`: none, one or two."""

    found: list[Instant] = []
    for fold in (0, 1):
        candidate = instant(wall.replace(tzinfo=PRAGUE, fold=fold))
        if candidate.astimezone(PRAGUE).replace(tzinfo=None) == wall and candidate not in found:
            found.append(candidate)
    return found


def _closest(readings: list[Instant], received_at: Instant) -> Instant | None:
    if not readings:
        return None
    return min(readings, key=lambda reading: (abs(reading - received_at), reading))


def resolve_local(wall: datetime, received_at: Instant) -> Instant | None:
    """A source's naive local date-time: the reading closest to `received_at`, or None if the
    time does not exist (the skipped spring hour)."""

    if wall.tzinfo is not None:
        raise TimeError(f"expected a naive local time, got {wall.isoformat()}")
    return _closest(_readings(wall), received_at)


def read_local(text: str, layout: str, received_at: Instant) -> Instant | None:
    """A source's local date-time text in `strptime` layout (SZ-Q2: `md`), resolved like
    `resolve_local`; None if it does not parse or does not exist."""

    try:
        wall = datetime.strptime(text.strip(), layout)  # noqa: DTZ007 - local, resolved below
    except ValueError:
        return None
    return resolve_local(wall, received_at)


def resolve_clock(clock: time, received_at: Instant) -> Instant | None:
    """A bare local time of day (`HH:mm`): the reading within 12 h of `received_at` closest to
    it, or None if no such reading exists."""

    readings: list[Instant] = []
    for day in candidate_service_dates(received_at):
        readings.extend(_readings(datetime.combine(day, clock)))
    near = [r for r in readings if abs(r - received_at) <= timedelta(hours=12)]
    return _closest(near, received_at)


def read_digits_as_utc(text: str) -> Instant:
    """An ISO time whose digits are UTC whatever offset it is labelled with (DUK-Q1)."""

    return instant(datetime.fromisoformat(text).replace(tzinfo=UTC))


def read_digits_as_local(text: str, received_at: Instant) -> Instant | None:
    """An ISO time whose digits are local time whatever offset it is labelled with
    (ARRIVA-Q1)."""

    return resolve_local(datetime.fromisoformat(text).replace(tzinfo=None), received_at)


def within_skew(value: Instant, received_at: Instant, max_skew: timedelta) -> bool:
    """Whether a source time is plausible: no further from reception than the policy allows."""

    return abs(value - received_at) <= max_skew
