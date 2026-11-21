"""The worker's time conversions (BASE_PLAN.md section 19.3), built on the edge in
`obehy.realtime_config`; nothing else converts realtime times.

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
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

# Prague time at the edge lives in `realtime_config` (read by the API and the jobs too, and
# kept after the switch to the Rust worker); the worker's conversions build on it.
from obehy.realtime_config import PRAGUE as PRAGUE
from obehy.realtime_config import Instant as Instant
from obehy.realtime_config import ServiceTime as ServiceTime
from obehy.realtime_config import TimeError as TimeError
from obehy.realtime_config import instant as instant
from obehy.realtime_config import local_date as local_date
from obehy.realtime_config import micros as micros
from obehy.realtime_config import parse_utc as parse_utc
from obehy.realtime_config import utc_text as utc_text

_HOUR_US = 3_600_000_000


def now() -> Instant:
    """The wall clock: the one place the realtime code reads it (live only; replay and warm
    start step a simulated clock)."""

    return instant(datetime.now(UTC))


def local_wall(value: Instant) -> datetime:
    """The Europe/Prague wall-clock time of an instant, naive: for comparing a time of day."""

    return value.astimezone(PRAGUE).replace(tzinfo=None)


def wall_instant(wall: datetime) -> Instant:
    """The instant of a Europe/Prague wall-clock time given without an offset (a command
    line's); the first of a repeated hour."""

    if wall.tzinfo is not None:
        return instant(wall)
    return instant(wall.replace(tzinfo=PRAGUE, fold=0))


def candidate_service_dates(value: Instant) -> tuple[date, date, date]:
    """Service dates an observation can belong to: local yesterday, today and tomorrow."""

    return _candidate_dates(micros(value) // _HOUR_US)


# Prague's UTC offsets are whole hours, so the local dates are the same for a whole UTC hour:
# they are asked several times per observation and cached per hour (bounded).
@lru_cache(maxsize=1 << 12)
def _candidate_dates(hour: int) -> tuple[date, date, date]:
    today = local_date(instant(datetime.fromtimestamp(hour * 3600, UTC)))
    return today - timedelta(days=1), today, today + timedelta(days=1)


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
