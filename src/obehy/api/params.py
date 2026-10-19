"""Query parameters: parsing and the departure window. Pure, so the rules are unit-tested."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

from obehy.realtime.times import Instant, ServiceTime, local_date

# Schedule seconds count wall-clock time from local midnight; around a DST change they differ
# from elapsed time by up to an hour, so the seconds prefilter is widened by that much.
_DST_SLACK_S = 3600
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class ParameterError(ValueError):
    """A parameter that cannot be used; answered with 422."""


@dataclass(frozen=True, slots=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    @property
    def area(self) -> float:
        return (self.east - self.west) * (self.north - self.south)


def parse_bbox(text: str, max_area: float) -> BBox:
    """`minlon,minlat,maxlon,maxlat` in WGS 84, at most `max_area` square degrees."""

    try:
        west, south, east, north = (float(part) for part in text.split(","))
    except ValueError:
        raise ParameterError("bbox must be minlon,minlat,maxlon,maxlat") from None
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise ParameterError("bbox must be finite numbers")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ParameterError("bbox must be minlon,minlat,maxlon,maxlat with min < max")
    box = BBox(west, south, east, north)
    if box.area > max_area:
        raise ParameterError(f"bbox is larger than {max_area} square degrees; zoom in")
    return box


def search_words(text: str) -> list[str]:
    """The words of a stop search: runs of letters and digits, so never a LIKE wildcard
    (`Česká Lípa,,aut.` is three)."""

    return _WORD.findall(text)


@dataclass(frozen=True, slots=True)
class Spans:
    """Per service date, the scheduled seconds that can fall in a window."""

    dates: list[date]
    lo: list[int]
    hi: list[int]


def schedule_spans(since: Instant, until: Instant) -> Spans:
    """The service dates whose schedule times can lie in [since, until): from the day before
    `since` (times run past 24:00) to the day of `until`, each with its range of seconds."""

    spans = Spans([], [], [])
    day = local_date(since) - timedelta(days=1)
    last = local_date(until)
    while day <= last:
        midnight = ServiceTime(day, 0).instant()
        hi = math.ceil((until - midnight).total_seconds()) + _DST_SLACK_S
        if hi >= 0:
            spans.dates.append(day)
            spans.lo.append(max(0, math.floor((since - midnight).total_seconds()) - _DST_SLACK_S))
            spans.hi.append(hi)
        day += timedelta(days=1)
    return spans


def split_vehicle_id(vehicle_id: str) -> tuple[str, str]:
    """`<source>:<source_vehicle_id>`; the source has no colon, the vehicle ID may."""

    source, sep, rest = vehicle_id.partition(":")
    if not sep or not source or not rest:
        raise ParameterError("vehicle ID must be <source>:<source_vehicle_id>")
    return source, rest
