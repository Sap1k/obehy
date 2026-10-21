"""Compositions and the lookups that answer them (BASE_PLAN.md sections 18.2 and 28).

A composition is the formation of a train, asked for lazily: the API records a lookup request
and the broker (`runtime/lookups.py`) plans, fetches and decodes it through a lookup connector
(`sources/cd.py`). Compositions are not core state: they move no vehicle and no journey, and are
never replayed through the core; every upstream answer is archived and logged
(`rt.lookup_fetch`), so it can be decoded again.

A lookup is three steps, as the connectors declare them:

    plan(subject, view) -> UpstreamRequest | NotAvailable     pure
    fetch(request) -> bytes                                   the broker, under rate and budget
    decode(bytes, request) -> Units                           pure

The view is what the worker knows of the trip: its calls with their identifiers in source
namespaces (SR70), which of them it has left, and its keys (the train number).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from obehy.realtime.times import Instant

# The journey key a composition is kept under: the first of these a trip has (stable across
# timetable versions, unlike its trip ID), else `trip_id`. The API reads them by the same rule.
JOURNEY_NAMESPACES = ("czptt:tr", "cis:line_trip", "pid:gtfs_trip_id")
UnitKind = Literal["locomotive", "coach", "unit"]
# What a unit is in the train; a source's own code with no home here is kept as `raw:<code>`.
UnitRole = Literal["train", "attached", "pusher"]
Outcome = Literal["answered", "not_found", "not_available", "failed"]


@dataclass(frozen=True, slots=True)
class Subject:
    """What the caller asks about: a trip on a date, from a call onward (None: from the next
    passenger call)."""

    feed: str
    trip_id: str
    service_date: date
    from_location: str | None = None


@dataclass(frozen=True, slots=True)
class ViewCall:
    ordinal: int  # 0-based along the trip
    location_id: str
    visit_n: int
    passenger: bool
    left: bool  # live state says the train has left it
    keys: Mapping[str, str]  # namespace -> identifier (sr70)


@dataclass(frozen=True, slots=True)
class TripView:
    feed: str
    trip_id: str
    service_date: date
    now: Instant
    keys: Mapping[str, str]  # namespace -> identifier: czptt:train_number, czptt:tr
    calls: tuple[ViewCall, ...]


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    """One upstream call. `cache_key` names the answer: subjects planned to the same key share
    one fetch. `params` fill the manifest URL's `{name}` placeholders. The answer holds from the
    call `start` onward."""

    cache_key: str
    params: tuple[tuple[str, str], ...]
    start: ViewCall


@dataclass(frozen=True, slots=True)
class NotAvailable:
    reason: str


@dataclass(frozen=True, slots=True)
class Unit:
    """One vehicle of a train as a source states it: an exact vehicle, a vehicle type, or
    neither (a locomotive the source does not name). `features` are vocabulary attributes
    (`data/vehicles/attributes.toml`) or `raw:<source>:<code>`; `texts` are the source's own
    words for its codes."""

    sequence: int
    kind: UnitKind
    role: str = "train"
    vehicle_type: str | None = None
    coach_number: str | None = None
    classes: tuple[int, ...] = ()
    features: tuple[str, ...] = ()
    texts: tuple[tuple[str, str], ...] = ()  # (code, the source's text)
    destination: str | None = None
    details: tuple[tuple[str, str], ...] = ()


def start_call(subject: Subject, view: TripView) -> ViewCall | NotAvailable:
    """The call an answer is asked from: the subject's, else the next passenger call the train
    has not left (the origin before it runs). Never the last passenger call: nothing departs
    from it."""

    passenger = [call for call in view.calls if call.passenger]
    if len(passenger) < 2:
        return NotAvailable("the trip has no passenger calls to depart from")
    departing = passenger[:-1]
    if subject.from_location is not None:
        named = [call for call in departing if call.location_id == subject.from_location]
        if not named:
            return NotAvailable(f"{subject.from_location} is no departing call of the trip")
        return next((call for call in named if not call.left), named[-1])
    last_left = max((call.ordinal for call in view.calls if call.left), default=-1)
    ahead = [call for call in departing if call.ordinal > last_left]
    if not ahead:
        return NotAvailable("the trip has left its last departing call")
    return ahead[0]
