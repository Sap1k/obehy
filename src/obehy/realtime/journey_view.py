"""The replay diagnostics' view of `journey_sample` records (`emit/samples.py`; the shapes are in
`compare.py`'s docstring): each journey of a sample with its whole calls and plan, rebuilt from
records that carry only what changed. Pure: it reads records, never the core, so the
diagnostics run the same on a stream written by Python or by the Rust worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast

from obehy.realtime.times import Instant, parse_utc

Record = dict[str, Any]


class Key(NamedTuple):
    """A journey: feed, key namespace, key and service date (`YYYY-MM-DD`)."""

    feed: str
    namespace: str
    key: str
    service_date: str


def journey_key(data: Record) -> Key:
    return Key(data["feed"], data["namespace"], data["key"], data["service_date"])


def at(value: object) -> Instant | None:
    return None if value is None else parse_utc(cast(str, value))


@dataclass(frozen=True, slots=True)
class Call:
    arrival: Instant | None  # the arrival event (its interval's middle)
    departure: Instant | None
    estimated_arrival: Instant | None
    estimated_departure: Instant | None
    passed_arrival: Instant | None
    passed_departure: Instant | None
    passenger: bool
    status: str


@dataclass(frozen=True, slots=True)
class Plan:
    scheduled: tuple[tuple[Instant | None, Instant | None], ...]
    zones: tuple[tuple[float | None, float | None], ...]  # (arrival_m, departure_m)
    calls_m: tuple[float, ...]
    shaped: bool
    points: int


@dataclass(frozen=True, slots=True)
class Track:
    seen_at: Instant | None
    at_stop: bool  # at a call, not seen away from it yet
    lateness_s: float | None
    hypotheses: bool
    off_path: bool  # the best reading is held off the path


@dataclass(frozen=True, slots=True)
class Fix:
    """Distances from the lead vehicle's position (`best_m`: to the best reading's point)."""

    best_m: float | None
    first_m: float
    last_m: float
    near: tuple[int, ...]  # calls around the reached one within the departure margin


@dataclass(frozen=True, slots=True)
class Journey:
    key: Key
    trip_id: str
    mode: str
    lifecycle: str
    stale: bool
    lost: bool
    rail: bool  # a rail run (parts): events come from sources
    lead: bool
    progress: tuple[int, float] | None  # (call index reached, along_m)
    track: Track | None
    fix: Fix | None
    calls: tuple[Call, ...]
    plan: Plan

    @property
    def reached(self) -> int:
        return self.progress[0] if self.progress is not None else -1


def _call(data: Record) -> Call:
    return Call(
        at(data["arrival"]),
        at(data["departure"]),
        at(data["estimated_arrival"]),
        at(data["estimated_departure"]),
        at(data["passed_arrival"]),
        at(data["passed_departure"]),
        bool(data["passenger"]),
        str(data["status"]),
    )


def _plan(data: Record) -> Plan:
    return Plan(
        tuple((at(a), at(d)) for a, d in data["scheduled"]),
        tuple((a, d) for a, d in data["zones"]),
        tuple(data["calls_m"]),
        bool(data["shaped"]),
        int(data["points"]),
    )


@dataclass(slots=True)
class JourneyView:
    """Each journey's calls and plan as of its last sample; a journey absent from a sample of
    its feed is forgotten (as the writer of the records forgets it)."""

    calls: dict[Key, list[Call]] = field(default_factory=dict[Key, list[Call]])
    plans: dict[Key, Plan] = field(default_factory=dict[Key, Plan])

    def read(self, record: Record) -> list[Journey]:
        journeys: list[Journey] = []
        present: set[Key] = set()
        for item in cast(list[Record], record["journeys"]):
            key = journey_key(item["journey"])
            present.add(key)
            calls = self.calls.get(key)
            if calls is None or len(calls) != item["n"]:
                calls = self.calls[key] = []
            for i, data in item["calls"]:
                call = _call(data)
                if i == len(calls):
                    calls.append(call)
                else:
                    calls[i] = call
            if item["plan"] is not None:
                self.plans[key] = _plan(item["plan"])
            journeys.append(_journey(key, item, tuple(calls), self.plans[key]))
        feed = record["feed"]
        for key in [k for k in self.calls if k.feed == feed and k not in present]:
            del self.calls[key]
            self.plans.pop(key, None)
        return journeys


def _journey(key: Key, item: Record, calls: tuple[Call, ...], plan: Plan) -> Journey:
    progress = item["progress"]
    track = item["track"]
    fix = item["fix"]
    return Journey(
        key,
        item["trip_id"],
        item["mode"],
        item["lifecycle"],
        item["stale"],
        item["lost"],
        item["rail"],
        item["lead"],
        None if progress is None else (progress["call"], progress["along_m"]),
        None
        if track is None
        else Track(
            at(track["seen_at"]),
            track["at_stop"],
            track["lateness_s"],
            track["hypotheses"],
            track["off_path"],
        ),
        None
        if fix is None
        else Fix(fix["best_m"], fix["first_m"], fix["last_m"], tuple(fix["near"])),
        calls,
        plan,
    )
