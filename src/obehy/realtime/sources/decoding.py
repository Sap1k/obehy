"""What every connector's `decode` does alike, so it is done one way: the payload as JSON, a
number or an integer from a JSON value, a source time within the clock skew of reception, a
usable position, and the CIS line-and-trip key.

A connector keeps only what is its source's own: the payload's shape, its time formats and its
quirks (each with its ID in the source's dossier).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

from obehy.realtime.facts import Position, TripKey
from obehy.realtime.times import Instant, within_skew


class PayloadError(ValueError):
    """A successful poll whose body does not have the source's documented shape."""


def load_json(body: bytes | str) -> object:
    try:
        return cast(object, json.loads(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PayloadError(f"not JSON: {error}") from error


def table(value: object) -> dict[str, Any]:
    """A JSON object, or an empty one for anything else."""

    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def number(value: object) -> float | None:
    """A JSON number or a numeric string; None for anything else (a boolean is no number)."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def integer(value: object) -> int | None:
    """A JSON integer or an integer string; None for anything else."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def fresh(observed: Instant | None, received_at: Instant, skew: timedelta) -> Instant | None:
    """A source time, unless further than the policy's clock skew from reception (T15): a time
    that far off is a wrong clock or a stale cached copy, and cannot place events."""

    if observed is None or not within_skew(observed, received_at, skew):
        return None
    return observed


def fix(lat: float | None, lon: float | None, bearing: float | None = None) -> Position | None:
    """A usable position: both coordinates, neither of them 0 (sources send 0, 0 for none)."""

    if lat is None or lon is None or lat == 0 or lon == 0:
        return None
    return Position(lat=lat, lon=lon, bearing=bearing)


def line_trip(line: int | str, trip: int | str) -> TripKey:
    """The CIS line and trip (`cis:line_trip`): the line as its six digits, the trip number
    without leading zeros, as the static feed's keys have them."""

    return TripKey("cis:line_trip", f"{int(line):06d}:{int(trip)}")
