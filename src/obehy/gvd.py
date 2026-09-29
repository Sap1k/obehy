"""Czech timetable year (GVD) boundaries in Europe/Prague."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")


def second_sunday(year: int) -> date:
    """Return the second Sunday of December, the first day of the next GVD."""
    value = date(year, 12, 1)
    return value + timedelta(days=(6 - value.weekday()) % 7 + 7)


def prague_today(now: datetime | None = None) -> date:
    return (now or datetime.now(PRAGUE)).astimezone(PRAGUE).date()


def automatic_timetable_year(now: datetime | None = None) -> int:
    """Return the GVD year active in Europe/Prague at *now*."""
    today = prague_today(now)
    return today.year + 1 if today >= second_sunday(today.year) else today.year


def resolve_timetable_year(value: int | Literal["auto"], now: datetime | None = None) -> int:
    return automatic_timetable_year(now) if value == "auto" else value
