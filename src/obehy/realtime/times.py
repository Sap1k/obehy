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

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
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

        return _schedule_instant(self.service_date, self.seconds)


@lru_cache(maxsize=1 << 16)  # schedule times repeat on every fix of a journey; bounded
def _schedule_instant(service_date: date, seconds: int) -> Instant:
    wall = datetime.combine(service_date, time()) + timedelta(seconds=seconds)
    return instant(wall.replace(tzinfo=PRAGUE, fold=0))


_EPOCH = Instant(datetime(1970, 1, 1, tzinfo=UTC))
_MICROSECOND = timedelta(microseconds=1)
_HOUR_US = 3_600_000_000


def local_date(value: Instant) -> date:
    """The Europe/Prague calendar date of an instant."""

    return _local_date(micros(value) // _HOUR_US)


def candidate_service_dates(value: Instant) -> tuple[date, date, date]:
    """Service dates an observation can belong to: local yesterday, today and tomorrow."""

    return _candidate_dates(micros(value) // _HOUR_US)


# Prague's UTC offsets are whole hours, so the local date is the same for a whole UTC hour:
# the conversions run several times per observation and are cached per hour (bounded).
@lru_cache(maxsize=1 << 12)
def _local_date(hour: int) -> date:
    return datetime.fromtimestamp(hour * 3600, UTC).astimezone(PRAGUE).date()


@lru_cache(maxsize=1 << 12)
def _candidate_dates(hour: int) -> tuple[date, date, date]:
    today = _local_date(hour)
    return today - timedelta(days=1), today, today + timedelta(days=1)


def micros(value: Instant) -> int:
    """Exact microseconds since the epoch, for arithmetic in the tracker's inner loop: a
    difference of two divided by 1_000_000 is bit-identical to `timedelta.total_seconds()`."""

    return (value - _EPOCH) // _MICROSECOND


def scaled_micros(delta_us: int, factor: float) -> int:
    """`delta_us` microseconds times `factor`, rounded as `timedelta * float` rounds (exactly,
    half to even)."""

    numerator, denominator = factor.as_integer_ratio()
    q, r = divmod(delta_us * numerator, denominator)
    r *= 2
    if r > denominator or (r == denominator and q % 2 == 1):
        q += 1
    return q


def next_multiple(at: Instant, every_s: int) -> Instant:
    """The first instant after `at` on a round multiple of `every_s` seconds (emit ticks)."""

    seconds = (int(at.timestamp()) // every_s + 1) * every_s
    return instant(datetime.fromtimestamp(seconds, UTC))


@lru_cache(maxsize=4096)  # a payload's vehicles share their seconds; bounded
def _readings(wall: datetime) -> tuple[Instant, ...]:
    """Every instant that reads as the naive local time `wall`: none, one or two."""

    found: list[Instant] = []
    for fold in (0, 1):
        candidate = instant(wall.replace(tzinfo=PRAGUE, fold=fold))
        if candidate.astimezone(PRAGUE).replace(tzinfo=None) == wall and candidate not in found:
            found.append(candidate)
    return tuple(found)


def _closest(readings: tuple[Instant, ...], received_at: Instant) -> Instant | None:
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
        fast = _LAYOUTS.get(layout)
        match = fast.fullmatch(text.strip()) if fast is not None else None
        if match is not None:
            a, month, c, hour, minute, second = (int(part) for part in match.groups())
            year, day = (c, a) if layout.startswith("%d") else (a, c)
            wall = datetime(year, month, day, hour, minute, second)  # noqa: DTZ001 - local
        else:
            wall = datetime.strptime(text.strip(), layout)  # noqa: DTZ007 - local, resolved below
    except ValueError:
        return None
    return resolve_local(wall, received_at)


# The layouts sources use on every entry, read without `strptime` (much slower); anything they
# do not match goes to `strptime`, which decides.
_LAYOUTS = {
    "%d.%m.%Y %H:%M:%S": re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4}) (\d{1,2}):(\d{1,2}):(\d{1,2})"),
    "%Y-%m-%dT%H:%M:%S": re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})T(\d{1,2}):(\d{1,2}):(\d{1,2})"),
}


def resolve_clock(clock: time, received_at: Instant) -> Instant | None:
    """A bare local time of day (`HH:mm`): the reading within 12 h of `received_at` closest to
    it, or None if no such reading exists."""

    readings: list[Instant] = []
    for day in candidate_service_dates(received_at):
        readings.extend(_readings(datetime.combine(day, clock)))
    near = [r for r in readings if abs(r - received_at) <= timedelta(hours=12)]
    return _closest(tuple(near), received_at)


def read_unless_offset_twice(text: str, reference: Instant, tolerance: timedelta) -> Instant | None:
    """An ISO time with its offset; when it is later than `reference` (a clock of the same
    device known to be right) by its own positive offset, within `tolerance`, the offset was
    applied twice (DUK-Q1) and is taken off once more. Only ever subtracted: a time behind
    its reference is just older. None without an offset."""

    moment = datetime.fromisoformat(text)
    offset = moment.utcoffset()
    if offset is None:
        return None
    labelled = instant(moment)
    if offset > timedelta(0) and abs(labelled - reference - offset) <= tolerance:
        return instant(labelled - offset)
    return labelled


def read_digits_as_local(text: str, received_at: Instant) -> Instant | None:
    """An ISO time whose digits are local time whatever offset it is labelled with
    (ARRIVA-Q1)."""

    return resolve_local(datetime.fromisoformat(text).replace(tzinfo=None), received_at)


def within_skew(value: Instant, received_at: Instant, max_skew: timedelta) -> bool:
    """Whether a source time is plausible: no further from reception than the policy allows."""

    return abs(value - received_at) <= max_skew
